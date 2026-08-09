"""Stratified-bucket regression model for learned consumption forecasting.

Buckets samples by ``(hour_of_day, workday/weekend, anyone_home)`` and fits a
simple linear regression of power (W) against outdoor temperature (°C) within
each bucket. Sparse buckets fall back to coarser aggregates (drop presence,
then day-type, then a single global regression, then a global mean) so
predictions stay sane before enough history has accumulated for a given
combination.

Pure data/math — no I/O. The join of raw measurement + signal history into
training samples happens in :func:`join_samples`; persistence and scheduling
live elsewhere (``core/learned_model_store.py`` and the server's recompute
loop).
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DEFAULT_MIN_SAMPLES_PER_BUCKET = 10

# Minimum temperature variance (°C²) within a bucket before its regression
# slope is trusted. Below this (e.g. all samples collected within a few
# hours, so the outdoor temperature barely moved), the slope is fit almost
# entirely to noise in y rather than a real x/y relationship, and
# extrapolates wildly for any query temperature outside that narrow band.
# ~1.0 °C² ≈ 1°C std dev — comfortably above sensor noise, well below the
# kind of day-to-day/seasonal spread needed to fit a meaningful slope.
MIN_TEMPERATURE_VARIANCE = 1.0

# (hour_of_day, "workday" | "weekend", anyone_home)
BucketKey = tuple[int, str, bool]

# (timestamp, temperature, power_w, anyone_home)
Sample = tuple[datetime, float, float, bool]


def day_type(ts: datetime) -> str:
    """Return ``"workday"`` for Mon–Fri, ``"weekend"`` for Sat/Sun."""
    return "weekend" if ts.weekday() >= 5 else "workday"


@dataclass
class RunningRegression:
    """Online-computable simple linear regression via running sums.

    Degrades gracefully to a plain mean when there's too little data or no
    variance in the independent variable.
    """

    n: int = 0
    sum_x: float = 0.0
    sum_y: float = 0.0
    sum_xy: float = 0.0
    sum_x2: float = 0.0
    min_y: float = float("inf")
    max_y: float = float("-inf")

    def add(self, x: float, y: float) -> None:
        self.n += 1
        self.sum_x += x
        self.sum_y += y
        self.sum_xy += x * y
        self.sum_x2 += x * x
        self.min_y = min(self.min_y, y)
        self.max_y = max(self.max_y, y)

    @property
    def mean_y(self) -> float:
        return self.sum_y / self.n if self.n else 0.0

    @property
    def variance_x(self) -> float:
        if self.n == 0:
            return 0.0
        mean_x = self.sum_x / self.n
        return self.sum_x2 / self.n - mean_x * mean_x

    def predict(self, x: float) -> float:
        if self.n == 0:
            return 0.0
        if self.n < 2 or self.variance_x < MIN_TEMPERATURE_VARIANCE:
            # Not enough spread in observed temperatures to trust a slope —
            # it would be fit almost entirely to noise. Fall back to mean.
            return self.mean_y
        denom = self.n * self.sum_x2 - self.sum_x * self.sum_x
        slope = (self.n * self.sum_xy - self.sum_x * self.sum_y) / denom
        mean_x = self.sum_x / self.n
        intercept = self.mean_y - slope * mean_x
        predicted = intercept + slope * x
        # Never extrapolate beyond what this bucket has actually observed —
        # a query temperature far outside the training range can otherwise
        # swing the regression to an implausible (even negative) value.
        return max(self.min_y, min(self.max_y, predicted))


@dataclass
class LearnedConsumptionModel:
    """Fitted bucketed regression model for a single device."""

    min_samples_per_bucket: int = DEFAULT_MIN_SAMPLES_PER_BUCKET

    _by_full: dict[BucketKey, RunningRegression] = field(default_factory=dict)
    _by_hour_daytype: dict[tuple[int, str], RunningRegression] = field(default_factory=dict)
    _by_hour: dict[int, RunningRegression] = field(default_factory=dict)
    _global: RunningRegression = field(default_factory=RunningRegression)
    _presence_prob: dict[tuple[int, str], float] = field(default_factory=dict)
    fitted_sample_count: int = 0

    def fit(self, samples: list[Sample]) -> None:
        """Rebuild all buckets from scratch (batch refit, not incremental)."""
        self._by_full = {}
        self._by_hour_daytype = {}
        self._by_hour = {}
        self._global = RunningRegression()
        presence_counts: dict[tuple[int, str], list[int]] = {}

        for ts, temperature, power_w, anyone_home in samples:
            # Bucket by local time, matching how predict() is queried (the
            # forecast plugin localizes via ts.astimezone() before calling
            # predict) — using the raw (usually UTC) hour here would bucket
            # training data under the wrong hour/day-type in any non-UTC zone.
            local_ts = ts.astimezone()
            hour = local_ts.hour
            dtype = day_type(local_ts)

            full_key: BucketKey = (hour, dtype, anyone_home)
            hd_key = (hour, dtype)

            self._by_full.setdefault(full_key, RunningRegression()).add(temperature, power_w)
            self._by_hour_daytype.setdefault(hd_key, RunningRegression()).add(temperature, power_w)
            self._by_hour.setdefault(hour, RunningRegression()).add(temperature, power_w)
            self._global.add(temperature, power_w)

            counts = presence_counts.setdefault(hd_key, [0, 0])
            counts[1] += 1
            if anyone_home:
                counts[0] += 1

        self._presence_prob = {
            key: (home_n / total_n if total_n else 0.5)
            for key, (home_n, total_n) in presence_counts.items()
        }
        self.fitted_sample_count = len(samples)

    def predict(self, hour: int, dtype: str, temperature: float) -> float:
        """Predict power (W) for the given hour/day-type/temperature.

        Blends the ``anyone_home`` True/False bucket predictions weighted by
        the historical presence probability for that (hour, day-type); falls
        back to coarser aggregates when a bucket is underpopulated.
        """
        min_n = self.min_samples_per_bucket
        hd_key = (hour, dtype)

        reg_home = self._by_full.get((hour, dtype, True))
        reg_away = self._by_full.get((hour, dtype, False))
        if reg_home is not None and reg_away is not None and reg_home.n >= min_n and reg_away.n >= min_n:
            p = self._presence_prob.get(hd_key, 0.5)
            return p * reg_home.predict(temperature) + (1 - p) * reg_away.predict(temperature)

        reg_hd = self._by_hour_daytype.get(hd_key)
        if reg_hd is not None and reg_hd.n >= min_n:
            return reg_hd.predict(temperature)

        reg_h = self._by_hour.get(hour)
        if reg_h is not None and reg_h.n >= min_n:
            return reg_h.predict(temperature)

        if self._global.n >= min_n:
            return self._global.predict(temperature)

        return self._global.mean_y


def nearest_value(
    sorted_points: list[tuple[datetime, float]],
    target: datetime,
    max_gap: timedelta,
) -> float | None:
    """Return the value of the point in *sorted_points* nearest to *target*,
    or ``None`` if empty or the nearest point is farther than *max_gap*.

    ``sorted_points`` must already be sorted by timestamp ascending — callers
    doing repeated lookups against the same series should sort once
    up-front rather than pay an O(n log n) sort per call.
    """
    if not sorted_points:
        return None
    sorted_ts = [t for t, _ in sorted_points]
    idx = bisect_left(sorted_ts, target)
    candidates = [i for i in (idx - 1, idx) if 0 <= i < len(sorted_ts)]
    if not candidates:
        return None
    best = min(candidates, key=lambda i: abs((sorted_ts[i] - target).total_seconds()))
    if abs((sorted_ts[best] - target).total_seconds()) > max_gap.total_seconds():
        return None
    return sorted_points[best][1]


def join_samples(
    measurements: list[tuple[datetime, float]],
    temperature_signal: list[tuple[datetime, float]],
    presence_signal: list[tuple[datetime, float]],
    max_gap: timedelta = timedelta(minutes=30),
) -> list[Sample]:
    """Join measurement power readings with the nearest temperature/presence
    signal sample within *max_gap*, dropping measurements with no match.

    This is a 1:1 nearest-timestamp join per sample — unlike
    ``_collect_forecasts`` (which sums same-quantity forecast points across
    devices), this never accumulates values across matches.
    """
    if not measurements or not temperature_signal or not presence_signal:
        return []

    # Pre-sort once rather than per nearest_value() call.
    sorted_temp = sorted(temperature_signal, key=lambda p: p[0])
    sorted_presence = sorted(presence_signal, key=lambda p: p[0])

    result: list[Sample] = []
    for ts, power_w in measurements:
        temperature = nearest_value(sorted_temp, ts, max_gap)
        presence = nearest_value(sorted_presence, ts, max_gap)
        if temperature is None or presence is None:
            continue
        result.append((ts, temperature, power_w, presence >= 0.5))
    return result

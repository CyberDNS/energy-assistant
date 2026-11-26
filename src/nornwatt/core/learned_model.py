"""Learned consumption model — daily energy × hourly shape, fitted from history.

The model is deliberately small and dependency-free.  It predicts the
consumption of one device in two stages:

1. **Daily energy** (kWh/day)

   * *temperature mode* — a hinge regression on the daily mean outdoor
     temperature::

         kwh = floor + slope · max(0, base − T)      (direction "heating")
         kwh = floor + slope · max(0, T − base)      (direction "cooling")

     Both directions are tried and the better fit wins, so the same code
     serves heat pumps and air conditioning.

   * *level mode* — used when no temperature history is available: the mean
     daily energy of the most recent days, per day type (weekday / weekend).

   On top of either, the previous day's prediction error is carried forward
   with a fitted decay factor ``rho`` — whatever temperature does not explain
   (setpoint changes, occupancy, …) tends to persist for a few days.

2. **Hourly shape** — the fraction of the daily energy that falls into each
   local hour, learned per regime (hinge active / idle) and day type.

Everything here is pure computation on plain Python data so it can be unit
tested without any backend.  Fetching history and temperature forecasts is
the job of ``plugins/learned``.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

# A day needs at least this many hourly values to count (23 covers the
# short DST day).
_MIN_HOURS_PER_DAY = 23
# … and this many hourly temperature values for a usable daily mean.
_MIN_TEMP_HOURS_PER_DAY = 20
# Hinge base temperatures are searched in steps of this many kelvin.
_BASE_STEP_K = 0.5
# A shape bucket needs this many days before it is trusted on its own.
_MIN_DAYS_PER_SHAPE = 7
# Days further than this many robust sigmas off the curve are treated as
# outliers and dropped before the final fit.
_OUTLIER_SIGMAS = 4.0
# Upper bound for the carry-forward factor; keeps a single odd day from
# dominating the forecast for a week.
_MAX_RHO = 0.9
# The carried-forward error is clamped to this many residual std-devs.
_MAX_CARRY_SIGMAS = 3.0


@dataclass(frozen=True)
class DaySample:
    """One complete local day of history."""

    day: date
    kwh: float
    #: Daily mean outdoor temperature (°C), ``None`` when unknown.
    temp: float | None
    #: Mean power (W) per local hour 0–23.
    hourly_w: tuple[float, ...]

    @property
    def day_type(self) -> str:
        return day_type(self.day)


def day_type(day: date) -> str:
    """``"weekday"`` (Mon–Fri) or ``"weekend"``."""
    return "weekday" if day.weekday() < 5 else "weekend"


def aggregate_days(
    power_w: dict[datetime, float],
    temp_c: dict[datetime, float] | None = None,
    excluded: set[datetime] | None = None,
    tz: tzinfo | None = None,
) -> list[DaySample]:
    """Group hourly history into complete local days.

    Parameters
    ----------
    power_w:
        Mean power in watts per hour, keyed by the (timezone-aware) start of
        the hour.
    temp_c:
        Mean outdoor temperature per hour, same keying.  Optional.
    excluded:
        Hour starts whose data must not be used.  A day containing any
        excluded hour is dropped entirely — its daily energy would be wrong.
    tz:
        Timezone that defines "a day".  ``None`` means the system local zone,
        matching how forecasts are queried.
    """
    excluded = excluded or set()
    by_day: dict[date, dict[int, list[float]]] = {}
    bad_days: set[date] = set()
    for ts, value in power_w.items():
        local = ts.astimezone(tz)
        if ts in excluded:
            bad_days.add(local.date())
            continue
        by_day.setdefault(local.date(), {}).setdefault(local.hour, []).append(value)
    # An excluded hour may have no power row at all — still poison its day.
    for ts in excluded:
        bad_days.add(ts.astimezone(tz).date())

    temp_by_day: dict[date, list[float]] = {}
    for ts, value in (temp_c or {}).items():
        temp_by_day.setdefault(ts.astimezone(tz).date(), []).append(value)

    out: list[DaySample] = []
    for day in sorted(by_day):
        if day in bad_days:
            continue
        hours = by_day[day]
        n_values = sum(len(v) for v in hours.values())
        if n_values < _MIN_HOURS_PER_DAY or len(hours) < _MIN_HOURS_PER_DAY:
            continue
        hourly = tuple(
            statistics.fmean(hours[h]) if h in hours else 0.0 for h in range(24)
        )
        mean_w = statistics.fmean(v for vals in hours.values() for v in vals)
        temps = temp_by_day.get(day, [])
        out.append(
            DaySample(
                day=day,
                kwh=mean_w * 24.0 / 1000.0,
                temp=statistics.fmean(temps) if len(temps) >= _MIN_TEMP_HOURS_PER_DAY else None,
                hourly_w=hourly,
            )
        )
    return out


@dataclass
class LearnedModel:
    """Fitted parameters.  JSON-serialisable via ``to_dict`` / ``from_dict``."""

    mode: str  # "temperature" | "level"
    # --- temperature mode -------------------------------------------------
    direction: str = "heating"  # "heating" | "cooling"
    base_temp: float = 0.0
    floor_kwh: float = 0.0
    slope_kwh_per_k: float = 0.0
    # --- level mode: day type (or "all") → kWh/day -------------------------
    level_kwh: dict[str, float] = field(default_factory=dict)
    # --- carry-forward ----------------------------------------------------
    rho: float = 0.0
    resid_std: float = 0.0
    last_residual: float = 0.0
    last_day: str | None = None  # ISO date of the day ``last_residual`` belongs to
    # --- hourly shape: key → 24 fractions summing to 1 ---------------------
    shapes: dict[str, list[float]] = field(default_factory=dict)
    # --- diagnostics ------------------------------------------------------
    n_days: int = 0
    n_outliers: int = 0
    outlier_days: list[str] = field(default_factory=list)  # ISO dates dropped by the robust pass
    r2: float = 0.0
    mae_kwh: float = 0.0
    recent_temp: float | None = None
    fitted_at: str | None = None  # ISO timestamp, set by the caller

    # ------------------------------------------------------------------ I/O

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LearnedModel":
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416
        return cls(**{k: v for k, v in data.items() if k in known})

    # ----------------------------------------------------------- prediction

    def hinge(self, temp: float) -> float:
        """Degrees beyond the base temperature in the active direction."""
        if self.direction == "cooling":
            return max(0.0, temp - self.base_temp)
        return max(0.0, self.base_temp - temp)

    def base_kwh(self, day: date, temp: float | None) -> float:
        """Daily energy before the carry-forward correction."""
        if self.mode == "temperature":
            t = temp if temp is not None else self.recent_temp
            if t is None:
                return self.floor_kwh
            return self.floor_kwh + self.slope_kwh_per_k * self.hinge(t)
        return self.level_kwh.get(day_type(day), self.level_kwh.get("all", 0.0))

    def daily_kwh(self, day: date, temp: float | None = None) -> float:
        """Predicted energy (kWh) for local *day*."""
        kwh = self.base_kwh(day, temp)
        if self.last_day and self.rho > 0.0:
            ahead = (day - date.fromisoformat(self.last_day)).days
            if ahead >= 1:
                limit = _MAX_CARRY_SIGMAS * self.resid_std
                carried = max(-limit, min(limit, self.last_residual))
                kwh += (self.rho**ahead) * carried
        return max(0.0, kwh)

    def observe(self, day: date, kwh: float, temp: float | None = None) -> None:
        """Record the actual energy of a completed *day*.

        Updates the carry-forward state without refitting, so the next
        prediction is corrected by this day's error.
        """
        self.last_residual = kwh - self.base_kwh(day, temp)
        self.last_day = day.isoformat()

    def shape(self, day: date, temp: float | None = None) -> list[float]:
        """24 hourly fractions for *day*, most specific bucket available."""
        dt = day_type(day)
        keys: list[str] = []
        if self.mode == "temperature":
            t = temp if temp is not None else self.recent_temp
            regime = "active" if t is not None and self.hinge(t) > 0.0 else "idle"
            keys += [f"{regime}|{dt}", regime]
        keys += [dt, "all"]
        for key in keys:
            if key in self.shapes:
                return self.shapes[key]
        return [1.0 / 24.0] * 24

    def power_kw(self, local_ts: datetime, temp: float | None = None) -> float:
        """Predicted mean power (kW) for the local hour containing *local_ts*.

        *temp* is the daily mean temperature expected for that local day.
        """
        day = local_ts.date()
        # fraction of the day's kWh in a one-hour slot == mean kW in that slot
        return self.daily_kwh(day, temp) * self.shape(day, temp)[local_ts.hour]


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def _fit_line(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares ``y = a + b·x`` with ``a ≥ 0`` and ``b ≥ 0``.

    Returns ``(a, b, sse)``.
    """
    n = len(xs)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx if sxx > 0 else 0.0
    b = max(0.0, b)
    a = my - b * mx
    if a < 0.0:
        # Refit through the origin rather than predict negative energy.
        a = 0.0
        sx2 = sum(x * x for x in xs)
        b = max(0.0, sum(x * y for x, y in zip(xs, ys)) / sx2) if sx2 > 0 else 0.0
    sse = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    return a, b, sse if n else 0.0


def _fit_hinge(
    temps: list[float], kwhs: list[float]
) -> tuple[str, float, float, float, float]:
    """Best hinge over both directions.  Returns (direction, base, floor, slope, sse)."""
    # Search bases across the whole observed range.  With only one season of
    # history the base is weakly identified, but a base near the warm end
    # extrapolates best into the season not yet seen.
    lo, hi = min(temps), max(temps)
    steps = max(1, int((hi - lo) / _BASE_STEP_K) + 1)
    best: tuple[str, float, float, float, float] | None = None
    for direction in ("heating", "cooling"):
        for i in range(steps + 1):
            base = lo + i * _BASE_STEP_K
            if direction == "heating":
                xs = [max(0.0, base - t) for t in temps]
            else:
                xs = [max(0.0, t - base) for t in temps]
            floor, slope, sse = _fit_line(xs, kwhs)
            if best is None or sse < best[4]:
                best = (direction, base, floor, slope, sse)
    assert best is not None
    return best


def _fit_rho(days: list[DaySample], residuals: list[float]) -> float:
    """Lag-1 regression of residuals over pairs of consecutive calendar days."""
    num = den = 0.0
    pairs = 0
    for i in range(1, len(days)):
        if (days[i].day - days[i - 1].day).days != 1:
            continue
        num += residuals[i] * residuals[i - 1]
        den += residuals[i - 1] ** 2
        pairs += 1
    if pairs < 10 or den <= 0.0:
        return 0.0
    return max(0.0, min(_MAX_RHO, num / den))


def _fit_shapes(days: list[DaySample], model: LearnedModel) -> dict[str, list[float]]:
    sums: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for d in days:
        total = sum(d.hourly_w)
        if total <= 0.0:
            continue
        keys = [d.day_type, "all"]
        if model.mode == "temperature" and d.temp is not None:
            regime = "active" if model.hinge(d.temp) > 0.0 else "idle"
            keys += [f"{regime}|{d.day_type}", regime]
        for key in keys:
            acc = sums.setdefault(key, [0.0] * 24)
            for h in range(24):
                # Average of per-day fractions: every day counts equally, so a
                # few very cold days do not dictate the shape.
                acc[h] += d.hourly_w[h] / total
            counts[key] = counts.get(key, 0) + 1
    shapes: dict[str, list[float]] = {}
    for key, acc in sums.items():
        if key != "all" and counts[key] < _MIN_DAYS_PER_SHAPE:
            continue
        s = sum(acc)
        shapes[key] = [v / s for v in acc] if s > 0 else [1.0 / 24.0] * 24
    return shapes


def fit(
    days: list[DaySample],
    *,
    min_days: int = 14,
    min_temp_days: int = 30,
    recency_days: int = 28,
) -> LearnedModel | None:
    """Fit a model to *days* (as returned by ``aggregate_days``).

    Returns ``None`` when there are fewer than *min_days* usable days.
    Temperature mode needs *min_temp_days* days with a known temperature;
    otherwise the fit falls back to level mode over the last *recency_days*.
    """
    days = sorted(days, key=lambda d: d.day)
    if len(days) < min_days:
        return None

    with_temp = [d for d in days if d.temp is not None]
    if len(with_temp) >= min_temp_days:
        used = with_temp
        temps = [d.temp for d in used]  # type: ignore[misc]
        kwhs = [d.kwh for d in used]
        direction, base, floor, slope, _ = _fit_hinge(temps, kwhs)  # type: ignore[arg-type]
        model = LearnedModel(
            mode="temperature", direction=direction, base_temp=base,
            floor_kwh=floor, slope_kwh_per_k=slope,
        )
        # One robust pass: drop days far off the curve (meter faults, guests,
        # a backup heater running) and refit.
        res = [d.kwh - model.base_kwh(d.day, d.temp) for d in used]
        sigma = 1.4826 * statistics.median(abs(r) for r in res)
        if sigma > 0.0:
            kept = [d for d, r in zip(used, res) if abs(r) <= _OUTLIER_SIGMAS * sigma]
            if min_temp_days <= len(kept) < len(used):
                model.n_outliers = len(used) - len(kept)
                kept_days = {d.day for d in kept}
                model.outlier_days = [d.day.isoformat() for d in used if d.day not in kept_days]
                used = kept
                direction, base, floor, slope, _ = _fit_hinge(
                    [d.temp for d in used], [d.kwh for d in used]  # type: ignore[misc]
                )
                model.direction, model.base_temp = direction, base
                model.floor_kwh, model.slope_kwh_per_k = floor, slope
        tail = used[-3:]
        model.recent_temp = statistics.fmean(d.temp for d in tail)  # type: ignore[misc]
    else:
        used = days
        cutoff = days[-1].day - timedelta(days=recency_days)
        recent = [d for d in days if d.day > cutoff] or days
        level = {"all": statistics.fmean(d.kwh for d in recent)}
        for dt in ("weekday", "weekend"):
            vals = [d.kwh for d in recent if d.day_type == dt]
            if len(vals) >= 4:
                level[dt] = statistics.fmean(vals)
        model = LearnedModel(mode="level", level_kwh=level)

    residuals = [d.kwh - model.base_kwh(d.day, d.temp) for d in used]
    mean_kwh = statistics.fmean(d.kwh for d in used)
    sst = sum((d.kwh - mean_kwh) ** 2 for d in used)
    sse = sum(r * r for r in residuals)
    model.n_days = len(used)
    model.r2 = 1.0 - sse / sst if sst > 0 else 0.0
    model.mae_kwh = statistics.fmean(abs(r) for r in residuals)
    model.resid_std = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    model.rho = _fit_rho(used, residuals)
    model.last_residual = residuals[-1]
    model.last_day = used[-1].day.isoformat()
    model.shapes = _fit_shapes(used, model)
    return model

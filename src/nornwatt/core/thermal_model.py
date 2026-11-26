"""Thermal model for temperature-driven devices — learned from history.

A threshold device (aquarium cooler, fridge, air conditioner, water heater,
dehumidifier …) keeps a measured value between two thresholds.  How that
value moves is described by a first-order model::

    d value / dt = offset + a · (ambient − value) + Σ_mode gain_mode · on_mode

* ``offset``  constant drift (value units per hour) — heat from a pump and
  lights, solar gain, or simply the drift towards a roughly constant ambient;
* ``a``       how fast the value is pulled towards the ambient (1/h,
  insulation and thermal mass).  Only with an ambient input ("ambient" form);
* ``gain``    what the device adds while running, per mode: negative for
  ``cool``, positive for ``heat``.  An air conditioner has both.

Without an ambient input the model is the familiar pair of constant rates:
drift when off (``offset``) and net change when on (``offset + gain``).

The model is linear in the value, so the MILP can step it slot by slot with
an ambient forecast (see ``plugins/milp_highs``).  Everything here is pure
computation on plain Python data; fetching history is the job of
``plugins/learned/thermal``.
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .models import ThresholdConstraints

COOL = "cool"
HEAT = "heat"

WINDOW = timedelta(minutes=15)
# A mode needs this many (partly) running windows, and the device this many
# fully idle windows, before a fit is trusted.
MIN_ON_WINDOWS = 8
MIN_OFF_WINDOWS = 16
# The ambient form is only considered when the ambient actually varies …
MIN_AMBIENT_SPREAD = 0.5
# … and only chosen when it explains clearly more than the constant form.
AMBIENT_GAIN = 0.95
# Windows further than this many robust sigmas off the first fit (a water
# change, a sensor glitch) are dropped before the final fit.
OUTLIER_SIGMAS = 4.0
# Floor for the residual spread (units/h): a near-perfect fit must not turn
# ordinary sensor steps into outliers.
MIN_RESIDUAL_SIGMA = 0.05


def device_mode(tc: ThresholdConstraints) -> str:
    """The mode a single-direction threshold device runs in."""
    return COOL if tc.direction == "reduces" else HEAT


@dataclass
class ThermalModel:
    """Parameters of one device's thermal model (see module docstring)."""

    offset: float
    gains: dict[str, float]
    a: float = 0.0
    form: str = "constant"
    """``"constant"`` (no ambient term) or ``"ambient"``."""
    source: str = "configured"
    """``"configured"`` (from the config's rates) or ``"learned"``."""
    fitted_at: str | None = None
    windows: int = 0
    off_windows: int = 0
    on_windows: dict[str, int] = field(default_factory=dict)
    rmse: float | None = None
    """Root-mean-square error of the fitted rate (value units per hour)."""
    ambient_mean: float | None = None
    """Mean ambient during the fit — the reference for the displayed rates."""
    outliers: int = 0

    def rate(self, value: float, ambient: float | None = None, on: Mapping[str, float] | None = None) -> float:
        """Rate of change (units/h) at *value* with modes *on* (fraction 0–1)."""
        r = self.offset
        if self.form == "ambient" and ambient is not None:
            r += self.a * (ambient - value)
        for mode, frac in (on or {}).items():
            r += self.gains.get(mode, 0.0) * frac
        return r

    def drift_rate(self, value: float, ambient: float | None = None) -> float:
        """Rate while off (units/h)."""
        return self.rate(value, ambient if ambient is not None else self.ambient_mean)

    def running_rate(self, mode: str, value: float, ambient: float | None = None) -> float:
        """Net rate while running in *mode* (units/h)."""
        amb = ambient if ambient is not None else self.ambient_mean
        return self.rate(value, amb, {mode: 1.0})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ThermalModel":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_constraints(cls, tc: ThresholdConstraints) -> "ThermalModel":
        """The constant-rate model the config describes."""
        if tc.direction == "reduces":
            offset = tc.drift_rate_per_h
            gain = -(tc.active_rate_per_h + tc.drift_rate_per_h)
        else:
            offset = -tc.drift_rate_per_h
            gain = tc.active_rate_per_h + tc.drift_rate_per_h
        return cls(offset=offset, gains={device_mode(tc): gain})


# ---------------------------------------------------------------------------
# Windows — fixed-length slices of history
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One fixed-length slice of history."""

    start: datetime
    hours: float
    v0: float
    v1: float
    on: dict[str, float]
    """Fraction of the window each mode was running."""
    ambient: float | None = None

    @property
    def slope(self) -> float:
        return (self.v1 - self.v0) / self.hours

    @property
    def v_mid(self) -> float:
        return (self.v0 + self.v1) / 2.0


class StepSeries:
    """A step-held time series: each value holds until the next event.
    ``None`` values mark gaps (sensor unavailable)."""

    def __init__(self, events: Iterable[tuple[datetime, Any]]) -> None:
        pts = sorted(events, key=lambda e: e[0])
        self._ts = [t for t, _ in pts]
        self._vals = [v for _, v in pts]

    def at(self, ts: datetime) -> Any:
        i = bisect.bisect_right(self._ts, ts) - 1
        return self._vals[i] if i >= 0 else None

    def changes_between(self, lo: datetime, hi: datetime) -> list[datetime]:
        i = bisect.bisect_right(self._ts, lo)
        j = bisect.bisect_left(self._ts, hi)
        return self._ts[i:j]

    def mean(self, lo: datetime, hi: datetime) -> float | None:
        """Time-weighted mean over [lo, hi); ``None`` if any part is a gap."""
        cuts = [lo, *self.changes_between(lo, hi), hi]
        total = 0.0
        for a, b in zip(cuts, cuts[1:]):
            v = self.at(a)
            if v is None:
                return None
            total += float(v) * (b - a).total_seconds()
        span = (hi - lo).total_seconds()
        return total / span if span > 0 else None


def build_windows(
    value: StepSeries,
    modes: Mapping[str, StepSeries],
    start: datetime,
    end: datetime,
    ambient: StepSeries | None = None,
    step: timedelta = WINDOW,
) -> list[Window]:
    """Cut history into windows; windows with a gap in value or mode are left out.

    *modes* map a mode to a step series of booleans (running or not).
    """
    out: list[Window] = []
    t = start
    hours = step.total_seconds() / 3600.0
    while t + step <= end:
        hi = t + step
        v0, v1 = value.at(t), value.at(hi)
        on: dict[str, float] = {}
        ok = v0 is not None and v1 is not None and not any(
            value.at(c) is None for c in value.changes_between(t, hi)
        )
        for mode, series in modes.items():
            frac = series.mean(t, hi) if ok else None
            if frac is None:
                ok = False
                break
            on[mode] = frac
        if ok:
            amb = ambient.mean(t, hi) if ambient is not None else None
            out.append(Window(t, hours, float(v0), float(v1), on, amb))
        t = hi
    return out


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def _lstsq(rows: Sequence[Sequence[float]], ys: Sequence[float]) -> list[float] | None:
    """Ordinary least squares via the normal equations (small problems only)."""
    n = len(rows[0])
    A = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    b = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(A[k][i]))
        if abs(A[p][i]) < 1e-12:
            return None
        A[i], A[p], b[i], b[p] = A[p], A[i], b[p], b[i]
        for k in range(i + 1, n):
            f = A[k][i] / A[i][i]
            A[k] = [x - f * y for x, y in zip(A[k], A[i])]
            b[k] -= f * b[i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (b[i] - sum(A[i][j] * x[j] for j in range(i + 1, n))) / A[i][i]
    return x


def _design(w: Window, modes: Sequence[str], with_ambient: bool) -> list[float]:
    row = [1.0] + [w.on.get(m, 0.0) for m in modes]
    if with_ambient:
        row.append((w.ambient or 0.0) - w.v_mid)
    return row


def _fit_form(
    windows: Sequence[Window], modes: Sequence[str], with_ambient: bool
) -> tuple[list[float], float, int] | None:
    """Fit one form with one round of outlier removal; ``(coef, rmse, outliers)``."""
    ws = list(windows)
    ys = [w.slope for w in ws]
    coef = _lstsq([_design(w, modes, with_ambient) for w in ws], ys)
    if coef is None:
        return None
    resid = [y - sum(c * x for c, x in zip(coef, _design(w, modes, with_ambient)))
             for w, y in zip(ws, ys)]
    mad = statistics.median(abs(r) for r in resid) * 1.4826
    limit = OUTLIER_SIGMAS * max(mad, MIN_RESIDUAL_SIGMA)
    keep = [i for i, r in enumerate(resid) if abs(r) <= limit]
    outliers = len(ws) - len(keep)
    if outliers and len(keep) >= 0.8 * len(ws):
        ws = [ws[i] for i in keep]
        ys = [ys[i] for i in keep]
        coef = _lstsq([_design(w, modes, with_ambient) for w in ws], ys)
        if coef is None:
            return None
    else:
        outliers = 0
    resid = [y - sum(c * x for c, x in zip(coef, _design(w, modes, with_ambient)))
             for w, y in zip(ws, ys)]
    rmse = math.sqrt(sum(r * r for r in resid) / len(resid))
    return coef, rmse, outliers


def fit(
    windows: Sequence[Window],
    modes: Sequence[str],
    fitted_at: datetime | None = None,
) -> tuple[ThermalModel | None, str]:
    """Fit a thermal model; returns ``(model, reason)`` — model ``None`` when
    the data cannot support one (*reason* says why)."""
    if not windows:
        return None, "no usable history"
    off = [w for w in windows if all(w.on.get(m, 0.0) <= 0.0 for m in modes)]
    on_counts = {m: sum(1 for w in windows if w.on.get(m, 0.0) > 0.0) for m in modes}
    if len(off) < MIN_OFF_WINDOWS:
        return None, f"only {len(off)} idle windows (need {MIN_OFF_WINDOWS})"
    for m, n in on_counts.items():
        if n < MIN_ON_WINDOWS:
            return None, f"only {n} windows with '{m}' running (need {MIN_ON_WINDOWS})"

    const = _fit_form(windows, modes, with_ambient=False)
    if const is None:
        return None, "the running and idle periods cannot be told apart"
    best, form = const, "constant"

    ambients = [w.ambient for w in windows if w.ambient is not None]
    if len(ambients) >= 0.9 * len(windows) and len(ambients) >= 2:
        spread = max(ambients) - min(ambients)
        amb_ws = [w for w in windows if w.ambient is not None]
        amb = _fit_form(amb_ws, modes, with_ambient=True) if spread >= MIN_AMBIENT_SPREAD else None
        if amb is not None and amb[0][-1] > 0 and amb[1] < AMBIENT_GAIN * const[1]:
            best, form = amb, "ambient"

    coef, rmse, outliers = best
    gains = {m: coef[1 + i] for i, m in enumerate(modes)}
    a = coef[-1] if form == "ambient" else 0.0
    model = ThermalModel(
        offset=coef[0], gains=gains, a=a, form=form, source="learned",
        fitted_at=fitted_at.isoformat() if fitted_at else None,
        windows=len(windows), off_windows=len(off), on_windows=on_counts,
        rmse=rmse, outliers=outliers,
        ambient_mean=statistics.fmean(ambients) if ambients else None,
    )
    # Physical sanity: a cooling mode must cool, a heating mode must heat.
    ref = statistics.fmean(w.v_mid for w in windows)
    for m in modes:
        net = model.running_rate(m, ref)
        if m == COOL and (gains[m] >= 0 or net >= 0):
            return None, f"fitted '{m}' does not cool (net {net:+.3f}/h)"
        if m == HEAT and (gains[m] <= 0 or net <= 0):
            return None, f"fitted '{m}' does not heat (net {net:+.3f}/h)"
    return model, "ok"


def simulate(
    model: ThermalModel,
    windows: Sequence[Window],
    reset_every: timedelta = timedelta(hours=6),
) -> list[float]:
    """Open-loop value at the end of each window, restarted from the measured
    value every *reset_every* (and after gaps) — for the Learning chart."""
    out: list[float] = []
    v: float | None = None
    anchor: datetime | None = None
    prev_end: datetime | None = None
    for w in windows:
        gap = prev_end is not None and w.start != prev_end
        if v is None or gap or anchor is None or w.start - anchor >= reset_every:
            v, anchor = w.v0, w.start
        v = v + w.hours * model.rate(v, w.ambient, w.on)
        out.append(v)
        prev_end = w.start + timedelta(hours=w.hours)
    return out

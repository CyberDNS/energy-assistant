"""Learned PV model — the roof learned from history, the sky from a weather forecast.

The model predicts the mean PV power of each hour from a weather forecast's
irradiance (global, direct normal and diffuse, W/m²).  Everything about the
installation is learned from the measured production, so no orientation,
tilt or peak power has to be configured::

    P = min(cap, k · shading(sun) · calibration(clearness) · snow · POA)

* **POA** — irradiance on the panels: the direct beam projected onto a plane
  of tilt ``tilt`` facing ``azimuth`` (0 = south, −90 = east, +90 = west),
  plus isotropic diffuse and ground-reflected light.  ``tilt``, ``azimuth``
  and the scale ``k`` (kW per W/m², i.e. peak power × system losses) are
  fitted by a grid search over all hours of history.
* **cap** — the highest hourly mean ever measured: the inverter limit.
* **shading** — a factor per sun position (10° azimuth × 5° elevation) from
  clear hours: what the best days reach there compared with the plane model.
  Only reductions are learned (trees, neighbouring roofs, low winter sun).
* **calibration** — the weather forecast's own bias per clearness (forecast
  global irradiance ÷ clear-sky irradiance) and sun height: e.g. too little
  light forecast under overcast skies.
* **snow** — while the forecast reports snow on the ground, production is
  scaled by the learned snow factor (panels covered).

Training excludes hours with snow on the ground and *suspect days*: cold
days the forecast called clear while the panels delivered almost nothing
(snow on the panels, fog).  They would teach the shading map and the
calibration that clear winter days are dark.

Everything here is pure computation on numpy arrays so it can be unit tested
without any backend.  Fetching history and forecasts is the job of
``plugins/learned_pv``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import numpy as np

# Hours with the sun lower than this (zenith above 88°) are night.
_MAX_ZENITH = math.radians(88.0)
# Grid of the plane search, then a fine search around the best point.
_TILTS = range(0, 65, 5)
_AZIMUTHS = range(-90, 95, 5)
_FINE = range(-4, 5)
# Hours at or above this share of the cap count as clipped (inverter limit).
_CLIPPED = 0.95
# Snow on the ground (m) and how long it keeps an hour out of training.
_SNOW_DEPTH_M = 0.005
_SNOW_HOURS = 24
# Shading map bins and the minimum clear hours per bin.
_AZ_BIN = 10.0
_EL_BIN = 5.0
_MIN_SHADING_HOURS = 10
_SHADING_QUANTILE = 0.8
# Calibration: clearness in tenths (0 … 1.2), sun below / above this height.
_LOW_SUN_DEG = 20.0
_MIN_CALIBRATION_HOURS = 20
_CALIBRATION_RANGE = (0.3, 3.0)
# Suspect days: forecast clear, panels below this share, colder than this.
_SUSPECT_RATIO = 0.4
_SUSPECT_TEMP_C = 3.0
_MIN_SNOW_HOURS = 10
# In-sample accuracy is reported over the most recent days.
_METRIC_DAYS = 60


@dataclass(frozen=True)
class WeatherSeries:
    """Hourly weather, one entry per hour starting at ``start[i]`` (UTC).

    Irradiance is the mean over that hour (W/m²), as the PV statistics are.
    """

    start: list[datetime]
    ghi: list[float]
    dni: list[float]
    dhi: list[float]
    temp: list[float | None] = field(default_factory=list)
    snow_depth: list[float | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.start)


def solar_position(starts: Sequence[datetime], latitude: float, longitude: float) -> tuple[np.ndarray, np.ndarray]:
    """Zenith and azimuth (radians; azimuth from south, + = west) at the
    middle of each hour starting at *starts* (UTC)."""
    mids = [s + timedelta(minutes=30) for s in starts]
    doy = np.array([m.timetuple().tm_yday for m in mids], dtype=float)
    hour = np.array([m.hour + m.minute / 60.0 for m in mids], dtype=float)
    g = 2 * np.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eot = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                    - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    dec = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g)
           - 0.006758 * np.cos(2 * g) + 0.000907 * np.sin(2 * g)
           - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    ha = np.radians((hour * 60 + eot + 4 * longitude) / 4 - 180)
    lat = math.radians(latitude)
    cos_z = np.sin(lat) * np.sin(dec) + np.cos(lat) * np.cos(dec) * np.cos(ha)
    zenith = np.arccos(np.clip(cos_z, -1.0, 1.0))
    azimuth = np.arctan2(np.sin(ha), np.cos(ha) * np.sin(lat) - np.tan(dec) * np.cos(lat))
    return zenith, azimuth


def _plane_irradiance(zen: np.ndarray, saz: np.ndarray, ghi: np.ndarray, dni: np.ndarray,
                      dhi: np.ndarray, tilt: float, azimuth: float) -> np.ndarray:
    """Irradiance on a plane (W/m²): beam + isotropic diffuse + ground (albedo 0.2)."""
    b, a = math.radians(tilt), math.radians(azimuth)
    cos_i = np.cos(zen) * math.cos(b) + np.sin(zen) * math.sin(b) * np.cos(saz - a)
    beam = dni * np.clip(cos_i, 0.0, None)
    return beam + dhi * (1 + math.cos(b)) / 2 + ghi * 0.2 * (1 - math.cos(b)) / 2


def _clearness(zen: np.ndarray, ghi: np.ndarray) -> np.ndarray:
    """Forecast global irradiance ÷ clear-sky global irradiance (Haurwitz)."""
    cos_z = np.clip(np.cos(zen), 0.02, None)
    clear = 1098.0 * cos_z * np.exp(-0.057 / cos_z)
    return np.clip(ghi / clear, 0.0, 1.2)


def _shading_key(az_rad: float, el_deg: float) -> str:
    return f"{math.floor(math.degrees(az_rad) / _AZ_BIN)},{math.floor(el_deg / _EL_BIN)}"


def _calibration_key(kt: float, el_deg: float) -> str:
    return f"{int(min(12, round(kt * 10)))},{0 if el_deg < _LOW_SUN_DEG else 1}"


@dataclass
class PvModel:
    """A fitted PV model (see module docstring)."""

    latitude: float
    longitude: float
    tilt: float
    azimuth: float
    k: float
    cap_kw: float
    shading: dict[str, float] = field(default_factory=dict)
    calibration: dict[str, float] = field(default_factory=dict)
    snow_factor: float = 1.0
    n_hours: int = 0
    n_days: int = 0
    n_snow_hours: int = 0
    suspect_days: list[str] = field(default_factory=list)
    mae_kw: float = 0.0
    mae_kwh: float = 0.0
    mean_kwh: float = 0.0
    fitted_at: str | None = None

    # ------------------------------------------------------------ predict

    def predict(self, weather: WeatherSeries) -> list[float]:
        """Mean PV power (kW) of each hour of *weather*."""
        if not len(weather):
            return []
        zen, saz = solar_position(weather.start, self.latitude, self.longitude)
        snow = np.where(snow_hours(weather), 1.0, 0.0)
        p = self._predict(zen, saz, np.nan_to_num(_arr(weather.ghi)), np.nan_to_num(_arr(weather.dni)),
                          np.nan_to_num(_arr(weather.dhi)), snow)
        return [float(v) for v in p]

    def _predict(self, zen: np.ndarray, saz: np.ndarray, ghi: np.ndarray, dni: np.ndarray,
                 dhi: np.ndarray, snow: np.ndarray | None = None, *,
                 shading: bool = True, calibration: bool = True) -> np.ndarray:
        p = self.k * _plane_irradiance(zen, saz, ghi, dni, dhi, self.tilt, self.azimuth)
        el = 90.0 - np.degrees(zen)
        if shading and self.shading:
            p = p * np.array([self.shading.get(_shading_key(a, e), 1.0) for a, e in zip(saz, el)])
        if calibration and self.calibration:
            kt = _clearness(zen, ghi)
            p = p * np.array([self.calibration.get(_calibration_key(c, e), 1.0) for c, e in zip(kt, el)])
        if snow is not None:
            p = np.where(np.nan_to_num(snow) > _SNOW_DEPTH_M, p * self.snow_factor, p)
        p = np.where(zen < _MAX_ZENITH, p, 0.0)
        return np.clip(p, 0.0, self.cap_kw)

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PvModel:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _arr(values: Sequence[float | None], n: int | None = None) -> np.ndarray:
    if n is not None and not values:
        return np.zeros(n)
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


def snow_hours(weather: WeatherSeries) -> np.ndarray:
    """True for hours with snow on the ground now or in the previous 24 h."""
    on_ground = {s for s, d in zip(weather.start, weather.snow_depth or [])
                 if d is not None and d > _SNOW_DEPTH_M}
    if not on_ground:
        return np.zeros(len(weather), dtype=bool)
    return np.array([
        any(s - timedelta(hours=h) in on_ground for h in range(_SNOW_HOURS + 1))
        for s in weather.start
    ], dtype=bool)


def fit(
    weather: WeatherSeries,
    pv_kw: Sequence[float],
    latitude: float,
    longitude: float,
    *,
    tz: tzinfo,
    cap_kw: float | None = None,
    min_days: int = 14,
) -> PvModel | None:
    """Fit a model on hours of *weather* with the measured mean power *pv_kw*.

    Returns None when fewer than *min_days* days have daylight hours.
    """
    if len(weather) != len(pv_kw):
        raise ValueError("weather and pv_kw must have the same length")
    if not len(weather):
        return None
    zen_all, saz_all = solar_position(weather.start, latitude, longitude)
    y_all = np.clip(_arr(pv_kw), 0.0, None)
    ghi_all, dni_all, dhi_all = _arr(weather.ghi), _arr(weather.dni), _arr(weather.dhi)
    temp_all = _arr(weather.temp, len(weather))
    snow_all = snow_hours(weather)
    ok = (zen_all < _MAX_ZENITH) & np.isfinite(y_all) & np.isfinite(ghi_all) \
        & np.isfinite(dni_all) & np.isfinite(dhi_all)
    idx = np.nonzero(ok)[0]
    local_day = [weather.start[i].astimezone(tz).date() for i in idx]
    if len(set(local_day)) < min_days:
        return None

    zen, saz, y = zen_all[idx], saz_all[idx], y_all[idx]
    ghi, dni, dhi, temp = ghi_all[idx], dni_all[idx], dhi_all[idx], temp_all[idx]
    snow = snow_all[idx]
    cap = float(cap_kw) if cap_kw else float(np.nanmax(y))
    if cap <= 0:
        return None

    # 1 — plane: tilt, azimuth and scale on hours without snow.
    train = ~snow
    tilt, azimuth, k = _fit_plane(zen[train], saz[train], ghi[train], dni[train], dhi[train],
                                  y[train], cap)
    model = PvModel(latitude=latitude, longitude=longitude, tilt=tilt, azimuth=azimuth,
                    k=k, cap_kw=round(cap, 3))
    plain = model._predict(zen, saz, ghi, dni, dhi, shading=False, calibration=False)

    # 2 — suspect days: forecast clear, cold, panels almost dark.
    kt = _clearness(zen, ghi)
    suspect = _suspect_days(local_day, kt, plain, y, temp)
    clean = train & np.array([d not in suspect for d in local_day])

    # 3 — shading: what the best clear hours reach per sun position.
    el = 90.0 - np.degrees(zen)
    clear = clean & (dni > 250) & (plain > 0.3) & (y < _CLIPPED * cap)
    bins: dict[str, list[float]] = {}
    for i in np.nonzero(clear)[0]:
        bins.setdefault(_shading_key(saz[i], el[i]), []).append(min(2.0, y[i] / plain[i]))
    model.shading = {
        key: round(min(1.0, float(np.quantile(vals, _SHADING_QUANTILE))), 3)
        for key, vals in bins.items() if len(vals) >= _MIN_SHADING_HOURS
    }

    # 4 — calibration of the forecast per clearness and sun height.
    shaded = model._predict(zen, saz, ghi, dni, dhi, calibration=False)
    sums: dict[str, list[float]] = {}
    for i in np.nonzero(clean & (shaded > 0.05))[0]:
        s = sums.setdefault(_calibration_key(kt[i], el[i]), [0.0, 0.0, 0])
        s[0] += y[i]
        s[1] += shaded[i]
        s[2] += 1
    lo, hi = _CALIBRATION_RANGE
    model.calibration = {
        key: round(float(min(hi, max(lo, s[0] / s[1]))), 3)
        for key, s in sums.items() if s[2] >= _MIN_CALIBRATION_HOURS and s[1] > 0
    }

    # 5 — snow: what the panels deliver while snow lies.
    full = model._predict(zen, saz, ghi, dni, dhi)
    on_snow = snow & (full > 0.3)
    if int(on_snow.sum()) >= _MIN_SNOW_HOURS:
        model.snow_factor = round(float(min(1.0, np.median(y[on_snow] / full[on_snow]))), 3)
    model.n_snow_hours = int(snow.sum())

    # 6 — in-sample accuracy over the most recent days.
    pred = model._predict(zen, saz, ghi, dni, dhi, np.where(snow, 1.0, 0.0))
    last = max(local_day)
    recent = np.array([d > last - timedelta(days=_METRIC_DAYS) for d in local_day])
    model.mae_kw = round(float(np.mean(np.abs(pred[recent] - y[recent]))), 3)
    daily: dict[date, list[float]] = {}
    for i in np.nonzero(recent)[0]:
        d = daily.setdefault(local_day[i], [0.0, 0.0])
        d[0] += pred[i]
        d[1] += y[i]
    model.mae_kwh = round(float(np.mean([abs(p - m) for p, m in daily.values()])), 2)
    model.mean_kwh = round(float(np.mean([m for _, m in daily.values()])), 2)
    model.n_hours = len(idx)
    model.n_days = len(set(local_day))
    model.suspect_days = sorted(d.isoformat() for d in suspect)
    return model


def _fit_plane(zen: np.ndarray, saz: np.ndarray, ghi: np.ndarray, dni: np.ndarray,
               dhi: np.ndarray, y: np.ndarray, cap: float) -> tuple[float, float, float]:
    unclipped = y < _CLIPPED * cap

    def score(tilt: float, azimuth: float) -> tuple[float, float]:
        x = _plane_irradiance(zen, saz, ghi, dni, dhi, tilt, azimuth)
        m = unclipped & (x > 20)
        den = float((x[m] ** 2).sum())
        k = float((x[m] * y[m]).sum() / den) if den > 0 else 0.0
        return float(np.mean(np.abs(np.minimum(cap, k * x) - y))), k

    best = min(((score(t, a), t, a) for t in _TILTS for a in _AZIMUTHS), key=lambda r: r[0][0])
    (_, _), t0, a0 = best
    fine = min(
        ((score(t, a), t, a) for dt in _FINE for da in _FINE
         if 0 <= (t := t0 + dt) <= 90 and -180 <= (a := a0 + da) <= 180),
        key=lambda r: r[0][0],
    )
    (_, k), tilt, azimuth = fine
    return float(tilt), float(azimuth), round(k, 7)


def _suspect_days(days: list[date], kt: np.ndarray, plain: np.ndarray, y: np.ndarray,
                  temp: np.ndarray) -> set[date]:
    ratios: dict[date, list[float]] = {}
    temps: dict[date, list[float]] = {}
    for i, d in enumerate(days):
        if np.isfinite(temp[i]):
            temps.setdefault(d, []).append(float(temp[i]))
        if kt[i] > 0.7 and plain[i] > 0.5:
            ratios.setdefault(d, []).append(float(y[i] / plain[i]))
    return {
        d for d, r in ratios.items()
        if float(np.median(r)) < _SUSPECT_RATIO
        and temps.get(d) and float(np.mean(temps[d])) < _SUSPECT_TEMP_C
    }

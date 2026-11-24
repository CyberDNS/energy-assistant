"""LearnedPvForecast — PV forecast from a model learned on the measured production.

The model (``core/pv_model.py``) is trained on Home Assistant's hourly
statistics of the PV power sensor against Open-Meteo's archived forecasts for
the same hours, refitted every ``refit_interval_h`` and persisted next to the
database.  The forecast applies it to Open-Meteo's current forecast.

Intraday correction
-------------------
A weather forecast misses fog, early sun and passing fronts.  The deviation
of the last hour (measured − model, kW) is therefore added to the coming
hours, fading with a time constant of ``_INTRADAY_TAU`` — backtested on the
history it reduces the error of the rest of the day by 7–12 % from late
morning on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ...core.models import ForecastPoint, ForecastQuantity
from ...core.pv_model import PvModel, WeatherSeries, fit
from .open_meteo import OpenMeteoClient

_log = logging.getLogger(__name__)

_RETRY_AFTER = timedelta(minutes=15)
_FORECAST_TTL = timedelta(minutes=30)
_FORECAST_RETRY = timedelta(minutes=5)
_INTRADAY_TAU_H = 1.5
_INTRADAY_WINDOW = timedelta(hours=1)
# Diagnostics show this many recent days of measured vs. model.
_DIAG_DAYS = 400


def _hour(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class LearnedPvForecast:
    """``ForecastProvider`` for PV generation learned from history."""

    def __init__(
        self,
        forecast_id: str,
        ha_client: Any,
        weather: OpenMeteoClient,
        *,
        latitude: float,
        longitude: float,
        history_entity: str,
        history_days: int = 730,
        refit_interval_h: float = 24.0,
        min_days: int = 14,
        capacity_kw: float | None = None,
        forecast_days: int = 7,
        intraday_correction: bool = True,
        device_id: str | None = None,
        fallback: Any = None,
        model_path: Path | None = None,
        tz: tzinfo | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._id = forecast_id
        self._ha = ha_client
        self._weather = weather
        self._lat = latitude
        self._lon = longitude
        self._history_entity = history_entity
        self._history = timedelta(days=history_days)
        self._refit_interval = timedelta(hours=refit_interval_h)
        self._min_days = min_days
        self._capacity_kw = capacity_kw
        self._forecast_days = forecast_days
        self._intraday = intraday_correction
        self._device_id = device_id
        self._fallback = fallback
        self._model_path = model_path
        self._tz = tz
        self._clock = clock or (lambda: datetime.now(UTC))

        self._model: PvModel | None = None
        self._loaded = False
        self._refit_task: asyncio.Task[None] | None = None
        self._next_attempt: datetime | None = None
        self._last_error: str | None = None
        self._forecast: tuple[datetime, WeatherSeries, list[float | None]] | None = None
        self._next_forecast: datetime | None = None
        self._lock = asyncio.Lock()
        # Training data of the latest fit (diagnostics): weather, measured kW.
        self._snapshot: tuple[WeatherSeries, list[float]] | None = None
        self._intraday_state: dict[str, Any] | None = None

    # ------------------------------------------------------------------ API

    @property
    def quantity(self) -> ForecastQuantity:
        return ForecastQuantity.PV_GENERATION

    @property
    def forecast_id(self) -> str:
        return self._id

    @property
    def model(self) -> PvModel | None:
        return self._model

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Hourly PV power (kW) from the current hour to now + *horizon*."""
        await self._ensure_model()
        model = self._model
        weather = await self._current_weather() if model is not None else None
        if model is None or weather is None:
            if self._fallback is not None:
                return await self._fallback.get_forecast(horizon)
            return []
        points, _ = await self._predict(model, weather, horizon)
        return points

    async def refit(self) -> PvModel | None:
        """Fetch history and archived forecasts, fit, persist.

        Raises on backend errors; returns None with too little history."""
        now = self._clock()
        tz = self._local_tz()
        start = now - self._history
        pv = await self._pv_history(start, now)
        if not pv:
            _log.warning("%s: no history for %s", self._id, self._history_entity)
            return None
        first = min(pv)
        weather = await self._weather.archive(
            first.astimezone(tz).date(), (now - timedelta(days=1)).astimezone(tz).date()
        )
        keep = [i for i, s in enumerate(weather.start) if s in pv and s < _hour(now)]
        train = WeatherSeries(
            [weather.start[i] for i in keep], [weather.ghi[i] for i in keep],
            [weather.dni[i] for i in keep], [weather.dhi[i] for i in keep],
            [weather.temp[i] for i in keep] if weather.temp else [],
            [weather.snow_depth[i] for i in keep] if weather.snow_depth else [],
        )
        measured = [pv[s] for s in train.start]
        model = await asyncio.to_thread(
            fit, train, measured, self._lat, self._lon,
            tz=tz, cap_kw=self._capacity_kw, min_days=self._min_days,
        )
        self._snapshot = (train, measured)
        if model is None:
            _log.warning("%s: only %d hours of usable history (need %d days) — model not updated",
                         self._id, len(train), self._min_days)
            return None
        model.fitted_at = self._clock().isoformat()
        self._model = model
        self._save(model)
        _log.info(
            "%s: fitted on %d days — tilt %.0f°, azimuth %+.0f°, %.2f kW per kW/m², cap %.2f kW, "
            "%d shading bins, snow factor %.2f, %d suspect days, MAE %.2f kWh/day",
            self._id, model.n_days, model.tilt, model.azimuth, model.k * 1000, model.cap_kw,
            len(model.shading), model.snow_factor, len(model.suspect_days), model.mae_kwh,
        )
        return model

    async def diagnostics(self, horizon: timedelta = timedelta(hours=48)) -> dict[str, Any]:
        """Model parameters, measured vs. model per day and the forecast."""
        await self._ensure_model()
        model = self._model
        tz = self._local_tz()
        if self._snapshot is None and model is not None:
            try:
                await self.refit()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
        days: list[dict[str, Any]] = []
        if model is not None and self._snapshot is not None:
            train, measured = self._snapshot
            pred = model.predict(train)
            suspect = set(model.suspect_days)
            per_day: dict[date, list[float]] = {}
            for s, p, m in zip(train.start, pred, measured):
                d = per_day.setdefault(s.astimezone(tz).date(), [0.0, 0.0])
                d[0] += m
                d[1] += p
            for d in sorted(per_day)[-_DIAG_DAYS:]:
                days.append({"date": d.isoformat(), "kwh": round(per_day[d][0], 2),
                             "model_kwh": round(per_day[d][1], 2),
                             "status": "suspect" if d.isoformat() in suspect else "used"})

        forecast: dict[str, Any] = {"points": [], "days": []}
        weather = await self._current_weather() if model is not None else None
        if model is not None and weather is not None:
            points, base = await self._predict(model, weather, horizon)
            forecast["points"] = [
                {"t": p.timestamp.isoformat(), "kw": round(p.value, 3), "model_kw": round(b, 3)}
                for p, b in zip(points, base)
            ]
            cloud = dict(zip(self._forecast[1].start, self._forecast[2])) if self._forecast else {}
            for p in forecast["points"]:
                c = cloud.get(datetime.fromisoformat(p["t"]))
                p["cloud"] = None if c is None else round(float(c))
            totals: dict[date, float] = {}
            for p in points:
                d = p.timestamp.astimezone(tz).date()
                totals[d] = totals.get(d, 0.0) + p.value
            forecast["days"] = [{"date": d.isoformat(), "kwh": round(v, 1)} for d, v in sorted(totals.items())]

        return {
            "id": self._id,
            "kind": "pv",
            "category": "pv",
            "label": "Solar forecast",
            "device_id": self._device_id,
            "history_entity": self._history_entity,
            "status": "fitted" if model else ("fallback" if self._fallback else "no_model"),
            "refitting": self._refit_task is not None and not self._refit_task.done(),
            "refit_interval_h": self._refit_interval.total_seconds() / 3600.0,
            "intraday_correction": self._intraday,
            "intraday": self._intraday_state,
            "last_error": self._last_error,
            "model": model.to_dict() if model else None,
            "days": days,
            "forecast": forecast,
        }

    # ------------------------------------------------------------ internals

    def _local_tz(self) -> tzinfo:
        return self._tz or datetime.now().astimezone().tzinfo or UTC

    async def _predict(
        self, model: PvModel, weather: WeatherSeries, horizon: timedelta
    ) -> tuple[list[ForecastPoint], list[float]]:
        """Hourly points from the current hour on, plus the uncorrected model values."""
        now = self._clock()
        current = _hour(now)
        cutoff = now + horizon
        base = model.predict(weather)
        by_hour = dict(zip(weather.start, base))
        delta = await self._intraday_delta(by_hour, now) if self._intraday else 0.0
        points: list[ForecastPoint] = []
        raw: list[float] = []
        for s, b in zip(weather.start, base):
            if s < current or s > cutoff:
                continue
            value = b
            if delta and b > 0:
                ahead_h = max(0.0, (s - now).total_seconds() / 3600.0)
                value = min(model.cap_kw, max(0.0, b + delta * math.exp(-ahead_h / _INTRADAY_TAU_H)))
            points.append(ForecastPoint(timestamp=s, value=value))
            raw.append(b)
        return points, raw

    async def _intraday_delta(self, by_hour: dict[datetime, float], now: datetime) -> float:
        """Measured minus model mean power over the last hour (kW)."""
        start = now - _INTRADAY_WINDOW
        try:
            stats = await self._ha.get_statistics(
                [self._history_entity], start=start - timedelta(minutes=5), end=now,
                period="5minute", types=("mean",), units={"power": "W"},
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("%s: no recent statistics for the intraday correction: %s", self._id, exc)
            return 0.0
        rows = [r for r in stats.get(self._history_entity, [])
                if r.get("mean") is not None and r["start"] >= start]
        if len(rows) < 6:
            self._intraday_state = None
            return 0.0
        values = [float(r["mean"]) for r in rows]
        sign = -1.0 if sum(values) < 0 else 1.0
        measured = max(0.0, sign * sum(values) / len(values) / 1000.0)
        model_vals = [by_hour.get(_hour(r["start"])) for r in rows]
        if any(v is None for v in model_vals):
            return 0.0
        modelled = sum(model_vals) / len(model_vals)  # type: ignore[arg-type]
        if modelled < 0.05 and measured < 0.05:
            self._intraday_state = None
            return 0.0
        delta = measured - modelled
        self._intraday_state = {"at": now.isoformat(), "measured_kw": round(measured, 3),
                                "model_kw": round(modelled, 3), "delta_kw": round(delta, 3)}
        return delta

    async def _current_weather(self) -> WeatherSeries | None:
        now = self._clock()
        async with self._lock:
            if self._next_forecast is not None and now < self._next_forecast:
                return self._forecast[1] if self._forecast else None
            try:
                weather, cloud = await self._weather.forecast(days=self._forecast_days, past_days=1)
            except Exception as exc:  # noqa: BLE001 — forecasting must never break planning
                _log.warning("%s: Open-Meteo forecast failed (%s) — retrying in %d min",
                             self._id, exc, _FORECAST_RETRY.seconds // 60)
                self._last_error = f"forecast: {exc}"
                self._next_forecast = now + _FORECAST_RETRY
                return self._forecast[1] if self._forecast else None
            self._forecast = (now, weather, cloud)
            self._next_forecast = now + _FORECAST_TTL
            return weather

    async def _pv_history(self, start: datetime, end: datetime) -> dict[datetime, float]:
        """Hourly mean PV power (kW, production positive) by hour start."""
        stats = await self._ha.get_statistics(
            [self._history_entity], start=start, end=end, period="hour",
            types=("mean",), units={"power": "W"},
        )
        rows = [(r["start"], float(r["mean"])) for r in stats.get(self._history_entity, [])
                if r.get("mean") is not None]
        # Some sensors report production as negative power.
        sign = -1.0 if sum(v for _, v in rows) < 0 else 1.0
        return {_hour(s): max(0.0, sign * v / 1000.0) for s, v in rows}

    async def _ensure_model(self) -> None:
        now = self._clock()
        if not self._loaded:
            self._loaded = True
            self._model = self._load()
        if self._next_attempt is not None and now < self._next_attempt:
            return
        if self._model is None:
            await self._guarded_refit()
            return
        fitted_at = datetime.fromisoformat(self._model.fitted_at) if self._model.fitted_at else None
        stale = fitted_at is None or now - fitted_at >= self._refit_interval
        if stale and (self._refit_task is None or self._refit_task.done()):
            self._refit_task = asyncio.create_task(self._guarded_refit(), name=f"refit-{self._id}")

    async def _guarded_refit(self) -> None:
        try:
            model = await self.refit()
            self._last_error = None if model is not None else "not enough usable history"
        except Exception as exc:  # noqa: BLE001 — forecasting must never break planning
            _log.warning("%s: refit failed (%s) — keeping previous model", self._id, exc)
            self._last_error = str(exc)
            model = None
        self._next_attempt = None if model is not None else self._clock() + _RETRY_AFTER

    def _load(self) -> PvModel | None:
        if self._model_path is None or not self._model_path.exists():
            return None
        try:
            model = PvModel.from_dict(json.loads(self._model_path.read_text()))
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: could not load persisted model: %s", self._id, exc)
            return None
        if (model.latitude, model.longitude) != (self._lat, self._lon):
            return None
        _log.info("%s: loaded persisted model fitted at %s", self._id, model.fitted_at)
        return model

    def _save(self, model: PvModel) -> None:
        if self._model_path is None:
            return
        try:
            self._model_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._model_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(model.to_dict(), indent=2))
            tmp.replace(self._model_path)
        except OSError as exc:
            _log.warning("%s: could not persist model: %s", self._id, exc)

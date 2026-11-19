"""LearnedConsumptionForecast — consumption forecast learned from HA statistics."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ...core.learned_model import DaySample, LearnedModel, aggregate_days, fit
from ...core.models import ForecastPoint, ForecastQuantity

_log = logging.getLogger(__name__)

# After a failed refit, wait this long before trying again.
_RETRY_AFTER = timedelta(minutes=10)
# Temperature inputs (weather forecast + today's observations) are cached
# this long; the plan is re-solved far more often than they change.
_TEMP_CACHE_TTL = timedelta(minutes=30)
# A local day needs this many hourly temperatures (observed + forecast)
# before its mean is trusted.
_MIN_TEMP_HOURS = 18
# Statistics are requested in these units so power sensors (hourly mean) and
# energy meters (hourly change) both read as watts.
_STAT_UNITS = {"power": "W", "energy": "Wh"}
_STAT_TYPES = ("mean", "min", "max", "change")
# A subtracted meter that last read below this (W) and then reports nothing
# is taken to be switched off, not broken.
_IDLE_W = 5.0


def hourly_watts(rows: list[dict[str, Any]], scale: float = 1.0) -> dict[datetime, float]:
    """Hourly mean power (W) from statistics rows, keyed by hour start.

    A power sensor contributes its hourly ``mean``; an energy meter (a
    statistic with a sum) its hourly ``change``, which in Wh equals the mean
    power over that hour.
    """
    out: dict[datetime, float] = {}
    for row in rows:
        value = row.get("mean")
        if value is None:
            value = row.get("change")
        if value is not None:
            out[row["start"]] = float(value) * scale
    return out


def residual_watts(
    target: dict[datetime, float],
    parts: dict[str, dict[datetime, float]],
) -> tuple[dict[datetime, float], set[datetime]]:
    """``target`` minus every series in *parts*, per hour.

    Returns ``(residual, gap_hours)``.  Before a part's first statistics row
    the part counts as zero (it was not metered yet, its load is part of the
    residual).  After that, an hour without a value is

    * zero when the part's last value before it was idle (below
      ``_IDLE_W``): a device switched off at the breaker often reports
      nothing at all until it is back;
    * a gap otherwise (a meter or recorder outage while the device was
      drawing power): the residual would be wrong, so the caller excludes
      the hour.

    The residual is clamped at zero; consumption cannot be negative, and
    small negative values only come from the meters not being sampled at
    the same instant.
    """
    first = {name: min(series) for name, series in parts.items() if series}
    last: dict[str, float] = {}
    residual: dict[datetime, float] = {}
    gaps: set[datetime] = set()
    for ts in sorted(target):
        value = target[ts]
        gap = False
        for name, series in parts.items():
            if name not in first or ts < first[name]:
                continue
            part = series.get(ts)
            if part is None:
                if last.get(name, 0.0) >= _IDLE_W:
                    gap = True
                continue
            last[name] = part
            value -= part
        if gap:
            gaps.add(ts)
        else:
            residual[ts] = max(0.0, value)
    return residual, gaps


@dataclass(frozen=True)
class ExcludeRange:
    """Half-open time range ``[start, end)`` to leave out of training."""

    start: datetime
    end: datetime

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


@dataclass(frozen=True)
class ExcludeRule:
    """Per-hour rule on another entity's statistics.

    Exactly one of the conditions applies:

    * ``frozen`` — the entity's min and max were identical for the whole hour
      (a stuck meter reading).
    * ``below`` / ``above`` — the entity's hourly mean crossed a threshold.

    ``min_hours`` requires the condition to hold for that many consecutive
    hours (for ``frozen``: at the same value) before any of them is excluded.
    Use it for sensors that can legitimately sit still for an hour, such as
    a temperature reading.
    """

    entity: str
    frozen: bool = False
    below: float | None = None
    above: float | None = None
    min_hours: int = 1

    def matches(self, row: dict[str, Any]) -> bool:
        if self.frozen:
            lo, hi = row.get("min"), row.get("max")
            return lo is not None and hi is not None and lo == hi
        mean = row.get("mean")
        if mean is None:
            return False
        if self.below is not None and mean < self.below:
            return True
        return self.above is not None and mean > self.above

    def excluded_hours(self, rows: list[dict[str, Any]]) -> set[datetime]:
        """Hour starts in *rows* (hourly statistics) this rule excludes."""
        out: set[datetime] = set()
        run: list[dict[str, Any]] = []

        def flush() -> None:
            if len(run) >= self.min_hours:
                out.update(r["start"] for r in run)
            run.clear()

        for row in sorted(rows, key=lambda r: r["start"]):
            if not self.matches(row):
                flush()
                continue
            if run and (
                row["start"] - run[-1]["start"] != timedelta(hours=1)
                or (self.frozen and row["min"] != run[-1]["min"])
            ):
                flush()
            run.append(row)
        flush()
        return out


def _parse_local(value: Any) -> datetime:
    """Parse a config timestamp; naive values are local time."""
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, date):
        ts = datetime(value.year, value.month, value.day)
    else:
        ts = datetime.fromisoformat(str(value).strip())
    return ts.astimezone() if ts.tzinfo is None else ts


def parse_exclude(raw: Any) -> list[ExcludeRange]:
    """Parse the ``exclude:`` config list (``{from: …, to: …}`` items)."""
    ranges: list[ExcludeRange] = []
    for item in raw or []:
        start, end = _parse_local(item["from"]), _parse_local(item["to"])
        if end <= start:
            raise ValueError(f"exclude range ends before it starts: {item!r}")
        ranges.append(ExcludeRange(start, end))
    return ranges


def parse_exclude_when(raw: Any) -> list[ExcludeRule]:
    """Parse the ``exclude_when:`` config list."""
    rules: list[ExcludeRule] = []
    for item in raw or []:
        condition = str(item.get("condition", "")).lower()
        below, above = item.get("below"), item.get("above")
        if condition not in ("", "frozen"):
            raise ValueError(f"unknown exclude_when condition: {condition!r}")
        if not (condition == "frozen" or below is not None or above is not None):
            raise ValueError(f"exclude_when rule has no condition: {item!r}")
        rules.append(
            ExcludeRule(
                entity=str(item["entity"]),
                frozen=condition == "frozen",
                below=float(below) if below is not None else None,
                above=float(above) if above is not None else None,
                min_hours=max(1, int(item.get("min_hours", 1))),
            )
        )
    return rules


class LearnedConsumptionForecast:
    """Consumption forecast fitted on Home Assistant long-term statistics.

    The model (see ``core.learned_model``) is refitted periodically from the
    hourly statistics of ``history_entity``.  When ``temperature_entity`` is
    set, daily energy is regressed on outdoor temperature and the forecast
    uses ``weather_entity`` for the temperatures ahead.

    The first call fits synchronously (or loads the persisted model); later
    refits run in the background so planning never waits on Home Assistant.
    Until a model exists the optional *fallback* provider answers instead.

    Parameters
    ----------
    forecast_id:
        Used in log messages.
    ha_client:
        Object with ``get_statistics`` and ``get_weather_forecast``
        (``HAClient`` or a test fake).
    history_entity:
        Power sensor whose hourly statistics are the training target.
    history_unit:
        ``None`` (default): Home Assistant converts the statistics to watts,
        whatever unit the sensor has, and energy meters (kWh/Wh) are read
        as their hourly change.  ``"W"`` or ``"kW"`` reads the hourly mean
        as is, scaled from that unit (for sensors without a unit).
    subtract:
        Further power sensors or energy meters whose hourly values are
        subtracted from ``history_entity`` before fitting.  With them the
        model learns a *residual*, e.g. the household meter minus every
        consumer that has its own forecast, so nothing is counted twice.
    temperature_entity:
        Outdoor temperature sensor (°C) with long-term statistics.  Optional.
    weather_entity:
        ``weather.*`` entity providing an hourly temperature forecast.
        Optional; without it the most recent observed temperature is assumed
        to persist.
    history_days:
        How far back to train.
    refit_interval_h:
        How often to refit.
    exclude / exclude_when:
        Training exclusions, see ``ExcludeRange`` / ``ExcludeRule``.
    fallback:
        ``ForecastProvider`` used while no model is available.
    model_path:
        JSON file the fitted model is persisted to (optional).
    clock:
        Returns the current UTC time; injectable for tests.
    """

    def __init__(
        self,
        forecast_id: str,
        ha_client: Any,
        history_entity: str,
        *,
        history_unit: str | None = None,
        subtract: list[str] | None = None,
        temperature_entity: str | None = None,
        weather_entity: str | None = None,
        history_days: int = 730,
        refit_interval_h: float = 6.0,
        min_days: int = 14,
        exclude: list[ExcludeRange] | None = None,
        exclude_when: list[ExcludeRule] | None = None,
        fallback: Any = None,
        model_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if history_unit not in (None, "W", "kW"):
            raise ValueError(f"history_unit must be 'W' or 'kW', got {history_unit!r}")
        self._id = forecast_id
        self._ha = ha_client
        self._history_entity = history_entity
        self._subtract = [e for e in dict.fromkeys(subtract or []) if e != history_entity]
        # None → let HA convert to W / Wh; else scale the raw mean ourselves.
        self._to_w = None if history_unit is None else (1000.0 if history_unit == "kW" else 1.0)
        self._temperature_entity = temperature_entity
        self._weather_entity = weather_entity
        self._history = timedelta(days=history_days)
        self._refit_interval = timedelta(hours=refit_interval_h)
        self._min_days = min_days
        self._exclude = list(exclude or [])
        self._exclude_when = list(exclude_when or [])
        self._fallback = fallback
        self._model_path = model_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._model: LearnedModel | None = None
        self._loaded = False
        self._refit_task: asyncio.Task[None] | None = None
        self._next_attempt: datetime | None = None
        self._temp_cache: tuple[datetime, dict[date, float]] | None = None
        # (usable days, excluded days) of the latest history fetch — kept for
        # the diagnostics view.
        self._snapshot: tuple[list[DaySample], list[DaySample]] | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ API

    @property
    def quantity(self) -> ForecastQuantity:
        return ForecastQuantity.CONSUMPTION

    @property
    def forecast_id(self) -> str:
        return self._id

    @property
    def history_entity(self) -> str:
        return self._history_entity

    @property
    def subtract(self) -> list[str]:
        return list(self._subtract)

    @property
    def model(self) -> LearnedModel | None:
        """The current fitted model, if any (diagnostics / tests)."""
        return self._model

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Return hourly ForecastPoints (kW) from now to now + horizon."""
        await self._ensure_model()
        model = self._model
        if model is None:
            if self._fallback is not None:
                return await self._fallback.get_forecast(horizon)
            return []

        points, _ = await self._predict(model, horizon)
        return points

    async def _predict(
        self, model: LearnedModel, horizon: timedelta
    ) -> tuple[list[ForecastPoint], dict[date, float]]:
        """Hourly points plus the daily temperatures they were based on."""
        now = self._clock().replace(minute=0, second=0, microsecond=0)
        # +1 for a lookahead point so the optimizer can interpolate the last step
        n_hours = max(1, int(horizon.total_seconds() / 3600)) + 1
        stamps = [now + timedelta(hours=h) for h in range(n_hours)]
        temps = await self._daily_temps(model, {ts.astimezone().date() for ts in stamps})
        points = []
        for ts in stamps:
            local = ts.astimezone()
            points.append(
                ForecastPoint(timestamp=ts, value=model.power_kw(local, temps.get(local.date())))
            )
        return points, temps

    async def refit(self) -> LearnedModel | None:
        """Fetch history, fit, and (on success) replace and persist the model.

        Raises on backend errors; returns ``None`` when there is not enough
        usable history (the previous model is kept).
        """
        days, excluded_days = await self._fetch_history()
        self._snapshot = (days, excluded_days)
        model = fit(days, min_days=self._min_days)
        if model is None:
            _log.warning(
                "%s: only %d usable days of history (need %d) — model not updated",
                self._id, len(days), self._min_days,
            )
            return None
        model.fitted_at = self._clock().isoformat()
        self._model = model
        self._temp_cache = None
        self._save(model)
        if model.mode == "temperature":
            _log.info(
                "%s: fitted on %d days (%d outliers dropped) — %.1f kWh/day + %.2f kWh/K "
                "%s %.1f °C, R²=%.2f, MAE=%.1f kWh/day, carry-forward=%.2f",
                self._id, model.n_days, model.n_outliers, model.floor_kwh,
                model.slope_kwh_per_k, "below" if model.direction == "heating" else "above",
                model.base_temp, model.r2, model.mae_kwh, model.rho,
            )
        else:
            _log.info(
                "%s: fitted on %d days — level %s kWh/day, MAE=%.1f kWh/day, carry-forward=%.2f",
                self._id, model.n_days,
                {k: round(v, 1) for k, v in model.level_kwh.items()},
                model.mae_kwh, model.rho,
            )
        return model

    async def fetch_days(self) -> list[DaySample]:
        """Fetch hourly statistics and return the usable ``DaySample`` list."""
        return (await self._fetch_history())[0]

    async def _fetch_history(self) -> tuple[list[DaySample], list[DaySample]]:
        """Fetch hourly statistics; return ``(usable days, excluded days)``."""
        now = self._clock()
        ids = [self._history_entity, *self._subtract]
        if self._temperature_entity and self._temperature_entity not in ids:
            ids.append(self._temperature_entity)
        ids += [r.entity for r in self._exclude_when if r.entity not in ids]
        if self._to_w is None:
            stats = await self._ha.get_statistics(
                ids, start=now - self._history, period="hour",
                types=_STAT_TYPES, units=_STAT_UNITS,
            )
            scale = 1.0
        else:
            stats = await self._ha.get_statistics(ids, start=now - self._history, period="hour")
            scale = self._to_w

        power = hourly_watts(stats.get(self._history_entity, []), scale)
        gaps: set[datetime] = set()
        if self._subtract:
            power, gaps = residual_watts(
                power,
                {e: hourly_watts(stats.get(e, []), scale) for e in self._subtract},
            )
        temp = None
        if self._temperature_entity:
            temp = {
                row["start"]: row["mean"]
                for row in stats.get(self._temperature_entity, [])
                if row.get("mean") is not None
            }
        excluded = gaps | {
            ts for ts in power if any(r.contains(ts) for r in self._exclude)
        }
        for rule in self._exclude_when:
            excluded |= rule.excluded_hours(stats.get(rule.entity, []))
        # The running day is incomplete — never train on it.
        today = now.astimezone().date()
        days = [d for d in aggregate_days(power, temp, excluded) if d.day < today]
        usable = {d.day for d in days}
        # What the excluded days would have contributed — for inspection only.
        excluded_days = [
            d for d in aggregate_days(power, temp)
            if d.day < today and d.day not in usable
        ]
        _log.debug(
            "%s: %d hourly rows, %d excluded hours, %d usable days",
            self._id, len(power), len(excluded), len(days),
        )
        return days, excluded_days

    async def diagnostics(self, horizon: timedelta = timedelta(hours=48)) -> dict[str, Any]:
        """Everything needed to inspect the model: parameters, the training
        days with their status, and the current forecast.

        Fetches the history on demand when no snapshot is in memory (e.g. the
        model was loaded from disk after a restart).
        """
        await self._ensure_model()
        model = self._model
        if self._snapshot is None:
            try:
                self._snapshot = await self._fetch_history()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
        usable, excluded_days = self._snapshot or ([], [])

        outliers = set(model.outlier_days) if model else set()
        needs_temp = model is not None and model.mode == "temperature"

        def row(d: DaySample, status: str) -> dict[str, Any]:
            return {
                "date": d.day.isoformat(),
                "kwh": round(d.kwh, 2),
                "temp": round(d.temp, 1) if d.temp is not None else None,
                "model_kwh": round(model.base_kwh(d.day, d.temp), 2)
                if model and not (needs_temp and d.temp is None) else None,
                "status": status,
            }

        days = [
            row(
                d,
                "outlier" if d.day.isoformat() in outliers
                else "no_temp" if needs_temp and d.temp is None
                else "used",
            )
            for d in usable
        ] + [row(d, "excluded") for d in excluded_days]
        days.sort(key=lambda r: r["date"])

        forecast: dict[str, Any] = {"points": [], "days": []}
        if model is not None:
            points, temps = await self._predict(model, horizon)
            forecast["points"] = [
                {"t": p.timestamp.isoformat(), "kw": round(p.value, 3)} for p in points
            ]
            for day in sorted({p.timestamp.astimezone().date() for p in points}):
                temp = temps.get(day)
                forecast["days"].append({
                    "date": day.isoformat(),
                    "temp": round(temp, 1) if temp is not None else None,
                    "kwh": round(model.daily_kwh(day, temp), 1),
                })

        return {
            "id": self._id,
            "history_entity": self._history_entity,
            "subtract": list(self._subtract),
            "temperature_entity": self._temperature_entity,
            "weather_entity": self._weather_entity,
            "status": "fitted" if model else ("fallback" if self._fallback else "no_model"),
            "refitting": self._refit_task is not None and not self._refit_task.done(),
            "refit_interval_h": self._refit_interval.total_seconds() / 3600.0,
            "last_error": self._last_error,
            "model": model.to_dict() if model else None,
            "days": days,
            "forecast": forecast,
        }

    # ------------------------------------------------------------ internals

    async def _ensure_model(self) -> None:
        now = self._clock()
        if not self._loaded:
            self._loaded = True
            self._model = self._load()
        if self._next_attempt is not None and now < self._next_attempt:
            return

        if self._model is None:
            # Nothing to serve yet — fit now so the very first plan is useful.
            await self._guarded_refit()
            return

        fitted_at = datetime.fromisoformat(self._model.fitted_at) if self._model.fitted_at else None
        stale = fitted_at is None or now - fitted_at >= self._refit_interval
        if stale and (self._refit_task is None or self._refit_task.done()):
            self._refit_task = asyncio.create_task(
                self._guarded_refit(), name=f"refit-{self._id}"
            )

    async def _guarded_refit(self) -> None:
        try:
            model = await self.refit()
            self._last_error = None if model is not None else "not enough usable history"
        except Exception as exc:  # noqa: BLE001 — forecasting must never break planning
            _log.warning("%s: refit failed (%s) — keeping previous model", self._id, exc)
            self._last_error = str(exc)
            model = None
        # On failure (or too little data) back off; on success the model's
        # ``fitted_at`` drives the next refit.
        self._next_attempt = None if model is not None else self._clock() + _RETRY_AFTER

    async def _daily_temps(self, model: LearnedModel, days: set[date]) -> dict[date, float]:
        """Expected daily mean temperature for each local day in *days*."""
        if model.mode != "temperature" or not self._temperature_entity:
            return {}
        now = self._clock()
        if self._temp_cache is not None and now - self._temp_cache[0] < _TEMP_CACHE_TTL:
            cached = self._temp_cache[1]
            if days <= cached.keys():
                return cached

        by_hour: dict[datetime, float] = {}
        if self._weather_entity:
            try:
                for ts, value in await self._ha.get_weather_forecast(self._weather_entity):
                    by_hour[ts.replace(minute=0, second=0, microsecond=0)] = value
            except Exception as exc:  # noqa: BLE001
                _log.warning("%s: weather forecast unavailable: %s", self._id, exc)
        # Hours of today that already happened come from the sensor itself and
        # take precedence over the forecast.
        midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            stats = await self._ha.get_statistics(
                [self._temperature_entity], start=midnight, period="hour", types=("mean",)
            )
            for row in stats.get(self._temperature_entity, []):
                if row.get("mean") is not None:
                    by_hour[row["start"]] = row["mean"]
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: observed temperature unavailable: %s", self._id, exc)

        per_day: dict[date, list[float]] = {}
        for ts, value in by_hour.items():
            per_day.setdefault(ts.astimezone().date(), []).append(value)

        result: dict[date, float] = {}
        # Days the forecast does not cover inherit the latest known day,
        # starting from the most recent observed temperature.
        last = model.recent_temp
        for day in sorted(days):
            values = per_day.get(day, [])
            if len(values) >= _MIN_TEMP_HOURS:
                last = sum(values) / len(values)
            if last is not None:
                result[day] = last
        self._temp_cache = (now, result)
        return result

    def _load(self) -> LearnedModel | None:
        if self._model_path is None or not self._model_path.exists():
            return None
        try:
            model = LearnedModel.from_dict(json.loads(self._model_path.read_text()))
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: could not load persisted model: %s", self._id, exc)
            return None
        _log.info("%s: loaded persisted model fitted at %s", self._id, model.fitted_at)
        return model

    def _save(self, model: LearnedModel) -> None:
        if self._model_path is None:
            return
        try:
            self._model_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._model_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(model.to_dict(), indent=2))
            tmp.replace(self._model_path)
        except OSError as exc:
            _log.warning("%s: could not persist model: %s", self._id, exc)

"""ThermalLearner — learns a threshold device's thermal model from HA history.

Configured on the threshold asset (``assets:`` section)::

    aquarium_cooler:
      type: threshold
      device: aquarium_cooler
      ...                         # thresholds, rates (used as fallback)
      model: learned              # default; "fixed" keeps the configured rates
      learning:                   # all optional
        value_entity: sensor.aquarium_thermometer_temperature  # default: device entity_sensor
        switch_entity: switch.aquarium_cooler                  # default: device entity_switch
        modes:                    # instead of switch_entity, for two-mode devices (AC)
          cool: switch.ac_cooling
          heat: switch.ac_heating
        ambient_entity: sensor.living_room_temperature        # optional ambient input
        ambient_weather_entity: weather.home                  # optional ambient forecast
        history_days: 10          # HA keeps state history 10 days by default
        refit_interval_h: 6

History comes from Home Assistant's state history (full resolution), cut
into 15-minute windows and fitted with ``core/thermal_model.fit``.  Until a
fit succeeds — and whenever it fails — the configured rates are used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...core.models import ForecastPoint, ThresholdConstraints
from ...core.thermal_model import (
    StepSeries,
    ThermalModel,
    build_windows,
    device_mode,
    fit,
    simulate,
)

_log = logging.getLogger(__name__)

_ON_STATES = {"on", "true", "1", "heat", "cool", "heating", "cooling"}
_GAP_STATES = {"unavailable", "unknown", "none", ""}


def _bool_series(rows: list[tuple[datetime, str]]) -> StepSeries:
    return StepSeries(
        (ts, None if s.lower() in _GAP_STATES else (1.0 if s.lower() in _ON_STATES else 0.0))
        for ts, s in rows
    )


def _float_series(rows: list[tuple[datetime, str]]) -> StepSeries:
    def val(s: str) -> float | None:
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    return StepSeries((ts, val(s)) for ts, s in rows)


class ThermalLearner:
    """Keeps the learned thermal model of one threshold device up to date."""

    def __init__(
        self,
        constraints: ThresholdConstraints,
        ha_client: Any,
        *,
        value_entity: str,
        mode_entities: dict[str, str],
        ambient_entity: str | None = None,
        ambient_weather_entity: str | None = None,
        history_days: float = 10.0,
        refit_interval_h: float = 6.0,
        model_path: Path | None = None,
    ) -> None:
        self.constraints = constraints
        self._ha = ha_client
        self._value_entity = value_entity
        self._mode_entities = dict(mode_entities)
        self._ambient_entity = ambient_entity
        self._ambient_weather_entity = ambient_weather_entity
        self._history = timedelta(days=history_days)
        self._refit_interval = timedelta(hours=refit_interval_h)
        self._model_path = model_path
        self._configured = ThermalModel.from_constraints(constraints)
        self._learned: ThermalModel | None = self._load()
        self._last_attempt: datetime | None = None
        self._last_error: str | None = None
        self._snapshot: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    @property
    def device_id(self) -> str:
        return self.constraints.device_id

    @property
    def id(self) -> str:
        return f"thermal:{self.device_id}"

    # -- model -----------------------------------------------------------

    def model(self) -> ThermalModel:
        """The learned model, or the configured rates until one is fitted."""
        return self._learned or self._configured

    def schedule_refit(self) -> None:
        """Start a background refit when due — at most one at a time, so
        planning never waits for Home Assistant's history."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.maybe_refit())

    async def maybe_refit(self, now: datetime | None = None) -> None:
        """Refit when the last attempt is older than the refit interval."""
        now = now or datetime.now(timezone.utc)
        if self._last_attempt is not None and now - self._last_attempt < self._refit_interval:
            return
        await self.refit(now)

    async def refit(self, now: datetime | None = None) -> ThermalModel | None:
        """Fetch history and fit; keep the previous model when the fit fails."""
        now = now or datetime.now(timezone.utc)
        self._last_attempt = now
        try:
            windows = await self._windows(now)
        except Exception as exc:  # noqa: BLE001 — HA unreachable etc.
            self._last_error = f"history fetch failed: {exc}"
            _log.warning("%s: %s", self.id, self._last_error)
            return None
        model, reason = fit(windows, list(self._mode_entities), fitted_at=now)
        self._snapshot = {"windows": windows, "model": model or self.model()}
        if model is None:
            self._last_error = reason
            _log.info("%s: no thermal model fitted — %s", self.id, reason)
            return None
        self._learned = model
        self._last_error = None
        self._save()
        _log.info(
            "%s: thermal model fitted (%s): offset %+.3f/h, gains %s, a %.4f/h, rmse %.3f/h",
            self.id, model.form, model.offset,
            {m: round(g, 3) for m, g in model.gains.items()}, model.a, model.rmse or 0.0,
        )
        return model

    async def ambient_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Ambient over the planning horizon: the weather forecast if given,
        otherwise the latest ambient reading held."""
        if self.model().form != "ambient" or not self._ambient_entity:
            return []
        if self._ambient_weather_entity:
            try:
                pts = await self._ha.get_weather_forecast(self._ambient_weather_entity)
                if pts:
                    return [ForecastPoint(timestamp=t, value=v) for t, v in pts]
            except Exception as exc:  # noqa: BLE001
                _log.debug("%s: weather forecast failed: %s", self.id, exc)
        try:
            state = await self._ha.get_entity_state(self._ambient_entity)
            return [ForecastPoint(timestamp=datetime.now(timezone.utc), value=float(state))]
        except Exception:  # noqa: BLE001
            mean = self.model().ambient_mean
            if mean is None:
                return []
            return [ForecastPoint(timestamp=datetime.now(timezone.utc), value=mean)]

    # -- history -----------------------------------------------------------

    async def _windows(self, now: datetime):
        start = now - self._history
        ids = [self._value_entity, *self._mode_entities.values()]
        if self._ambient_entity:
            ids.append(self._ambient_entity)
        hist = await self._ha.get_history(ids, start=start, end=now)
        value = _float_series(hist.get(self._value_entity, []))
        modes = {m: _bool_series(hist.get(e, [])) for m, e in self._mode_entities.items()}
        ambient = _float_series(hist[self._ambient_entity]) if (
            self._ambient_entity and hist.get(self._ambient_entity)
        ) else None
        # Start on a window boundary so windows line up with plan slots.
        first = start.replace(second=0, microsecond=0)
        first += timedelta(minutes=(15 - first.minute % 15) % 15)
        return build_windows(value, modes, first, now, ambient)

    # -- diagnostics -------------------------------------------------------

    def diagnostics_reason(self) -> str:
        """Why the last fit failed (or ``"unknown"``)."""
        return self._last_error or "unknown"

    async def diagnostics(self) -> dict[str, Any]:
        """Learning-tab payload: model, configured rates, fit quality and the
        measured vs. modelled trajectory of the fitting window."""
        if self._snapshot is None:
            try:
                windows = await self._windows(datetime.now(timezone.utc))
                self._snapshot = {"windows": windows, "model": self.model()}
            except Exception as exc:  # noqa: BLE001
                self._last_error = self._last_error or f"history fetch failed: {exc}"
                self._snapshot = {"windows": [], "model": self.model()}
        windows = self._snapshot["windows"]
        model = self.model()
        sim = simulate(model, windows) if windows else []
        tc = self.constraints
        ref = (tc.bottom_threshold + tc.top_threshold) / 2.0
        mode = device_mode(tc)
        configured = self._configured
        return {
            "id": self.id,
            "kind": "thermal",
            "device_id": tc.device_id,
            "label": tc.label or tc.device_id,
            "unit": tc.unit,
            "direction": tc.direction,
            "bottom_threshold": tc.bottom_threshold,
            "top_threshold": tc.top_threshold,
            "status": "learned" if self._learned is not None else "fallback",
            "last_error": self._last_error,
            "entities": {
                "value": self._value_entity,
                "modes": self._mode_entities,
                "ambient": self._ambient_entity,
                "ambient_forecast": self._ambient_weather_entity,
            },
            "model": model.to_dict(),
            "rates": {
                "drift_per_h": model.drift_rate(ref),
                "running_per_h": {m: model.running_rate(m, ref) for m in model.gains},
                "reference_value": ref,
                "reference_ambient": model.ambient_mean,
            },
            "configured": {
                "drift_per_h": configured.drift_rate(ref),
                "running_per_h": {mode: configured.running_rate(mode, ref)},
            },
            # One point per window, at its end: measured value, the open-loop
            # model (restarted from the measured value every 6 h), and how
            # much of the window each mode ran.
            "series": [
                {
                    "t": (w.start + timedelta(hours=w.hours)).isoformat(),
                    "value": w.v1,
                    "model": round(s, 4),
                    "on": w.on,
                    "ambient": w.ambient,
                }
                for w, s in zip(windows, sim)
            ],
        }

    # -- persistence -------------------------------------------------------

    def _load(self) -> ThermalModel | None:
        if self._model_path is None or not self._model_path.exists():
            return None
        try:
            return ThermalModel.from_dict(json.loads(self._model_path.read_text()))
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: could not read %s: %s", self.id, self._model_path, exc)
            return None

    def _save(self) -> None:
        if self._model_path is None or self._learned is None:
            return
        try:
            self._model_path.parent.mkdir(parents=True, exist_ok=True)
            self._model_path.write_text(json.dumps(self._learned.to_dict()))
        except OSError as exc:
            _log.warning("%s: could not save %s: %s", self.id, self._model_path, exc)


def build_thermal_learners(
    constraints: list[ThresholdConstraints],
    raw_assets: dict[str, Any],
    devices: dict[str, dict[str, Any]],
    ha_client: Any,
    data_dir: Path | None,
) -> dict[str, ThermalLearner]:
    """One learner per threshold asset with ``model: learned`` (the default)."""
    by_device = {tc.device_id: tc for tc in constraints}
    out: dict[str, ThermalLearner] = {}
    for asset_id, cfg in (raw_assets or {}).items():
        if not isinstance(cfg, dict) or cfg.get("type") != "threshold":
            continue
        device_id = str(cfg.get("device") or asset_id)
        tc = by_device.get(device_id)
        if tc is None or str(cfg.get("model", "learned")) != "learned":
            continue
        if ha_client is None:
            _log.info("thermal:%s: learning needs the homeassistant backend — using configured rates", device_id)
            continue
        learn = cfg.get("learning") or {}
        dev = devices.get(device_id) or {}
        value_entity = learn.get("value_entity") or dev.get("entity_sensor")
        modes = learn.get("modes")
        if not isinstance(modes, dict) or not modes:
            switch = learn.get("switch_entity") or dev.get("entity_switch")
            modes = {device_mode(tc): switch} if switch else {}
        if not value_entity or not modes:
            _log.warning(
                "thermal:%s: no value/switch entity known (set learning.value_entity and "
                "learning.switch_entity) — using configured rates", device_id,
            )
            continue
        out[device_id] = ThermalLearner(
            tc, ha_client,
            value_entity=str(value_entity),
            mode_entities={str(m): str(e) for m, e in modes.items()},
            ambient_entity=learn.get("ambient_entity"),
            ambient_weather_entity=learn.get("ambient_weather_entity"),
            history_days=float(learn.get("history_days", 10)),
            refit_interval_h=float(learn.get("refit_interval_h", 6)),
            model_path=(data_dir / "learned_models" / f"thermal_{device_id}.json") if data_dir else None,
        )
    return out

"""LearnedConsumptionForecast — regression-based consumption forecast.

Reads temperature forecast points from Home Assistant and blends them with a
periodically refit :class:`~energy_assistant.core.learned_model.LearnedConsumptionModel`
(held in a shared :class:`~energy_assistant.core.learned_model_store.LearnedModelStore`)
to produce hourly ``ForecastPoint``s over the requested horizon.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ...core.learned_model import day_type
from ...core.models import ForecastPoint, ForecastQuantity

_log = logging.getLogger(__name__)

# Used when no weather forecast is configured/reachable — a mild fallback
# rather than 0°C, so the regression doesn't extrapolate wildly.
_DEFAULT_TEMPERATURE_C = 15.0


class LearnedConsumptionForecast:
    """Consumption forecast driven by a fitted ``LearnedConsumptionModel``.

    The model itself is fitted out-of-band by the server's recompute loop and
    published into *model_store*; this class only reads it and combines it
    with a weather forecast to produce forward-looking points.
    """

    def __init__(
        self,
        device_id: str,
        model_store: Any,
        ha_client: Any,
        environment: dict[str, Any],
    ) -> None:
        self._device_id = device_id
        self._model_store = model_store
        self._ha_client = ha_client
        self._environment = environment or {}
        self._warned_no_model = False

    @property
    def quantity(self) -> ForecastQuantity:
        return ForecastQuantity.CONSUMPTION

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Return hourly ForecastPoints (kW) from now to now + horizon."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        # +1 for a lookahead point, matching static_profile's convention.
        n_hours = max(1, int(horizon.total_seconds() / 3600)) + 1

        weather_points = await self._fetch_weather_forecast()
        model = self._model_store.get(self._device_id) if self._model_store else None
        if model is None and not self._warned_no_model:
            _log.warning(
                "No fitted learned_consumption model yet for %r — forecasting 0 "
                "until the recompute loop runs",
                self._device_id,
            )
            self._warned_no_model = True

        points: list[ForecastPoint] = []
        for h in range(n_hours):
            ts = now + timedelta(hours=h)
            local_ts = ts.astimezone()
            temperature = self._temperature_at(ts, weather_points)
            power_w = model.predict(local_ts.hour, day_type(local_ts), temperature) if model else 0.0
            points.append(ForecastPoint(timestamp=ts, value=max(0.0, power_w) / 1000.0))
        return points

    async def _fetch_weather_forecast(self) -> list[tuple[datetime, float]]:
        entity_id = self._environment.get("weather")
        if not entity_id or self._ha_client is None:
            return []
        try:
            raw = await self._ha_client.get_weather_forecast(entity_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Could not fetch weather forecast from %r: %s", entity_id, exc)
            return []

        points: list[tuple[datetime, float]] = []
        for p in raw:
            dt_str = p.get("datetime") or p.get("date_time")
            temp = p.get("temperature")
            if dt_str is None or temp is None:
                continue
            try:
                ts = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
            except ValueError:
                continue
            points.append((ts, float(temp)))
        points.sort(key=lambda pt: pt[0])
        return points

    def _temperature_at(
        self, ts: datetime, weather_points: list[tuple[datetime, float]]
    ) -> float:
        if not weather_points:
            return _DEFAULT_TEMPERATURE_C
        best = min(weather_points, key=lambda pt: abs((pt[0] - ts).total_seconds()))
        return best[1]

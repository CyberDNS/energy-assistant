"""Fake Home Assistant client for unit tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class FakeHAClient:
    """In-memory HAClient stub that implements HAClientProtocol."""

    def __init__(
        self,
        states: dict[str, Any] | None = None,
        entities: dict[str, dict[str, Any]] | None = None,
        forecasts: dict[str, list[dict[str, Any]]] | None = None,
        history: dict[str, list[tuple[datetime, str]]] | None = None,
    ) -> None:
        self._states: dict[str, Any] = dict(states or {})
        # Full state objects (state + attributes) keyed by entity_id, for get_entity().
        self._entities: dict[str, dict[str, Any]] = dict(entities or {})
        # Weather forecast points keyed by entity_id, for get_weather_forecast().
        self._forecasts: dict[str, list[dict[str, Any]]] = dict(forecasts or {})
        # (timestamp, raw_state) history keyed by entity_id, for get_history().
        self._history: dict[str, list[tuple[datetime, str]]] = dict(history or {})
        # Records every call_service invocation as (domain, service, data).
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get_entity_state(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        return self._entities.get(entity_id, {"state": self._states.get(entity_id), "attributes": {}})

    async def get_weather_forecast(
        self, entity_id: str, forecast_type: str = "hourly"
    ) -> list[dict[str, Any]]:
        return list(self._forecasts.get(entity_id, []))

    async def get_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        return [
            (ts, state)
            for ts, state in self._history.get(entity_id, [])
            if start <= ts <= end
        ]

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append((domain, service, data or {}))

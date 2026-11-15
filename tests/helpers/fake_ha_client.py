"""Fake Home Assistant client for unit tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class FakeHAClient:
    """In-memory HAClient stub that implements HAClientProtocol."""

    def __init__(self, states: dict[str, Any] | None = None) -> None:
        self._states: dict[str, Any] = dict(states or {})
        # Records every call_service invocation as (domain, service, data).
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        # statistic_id -> rows ({"start": datetime, "mean": ..., "min": ..., "max": ...})
        self.statistics: dict[str, list[dict[str, Any]]] = {}
        # weather entity_id -> [(timestamp, temperature)]
        self.weather: dict[str, list[tuple[datetime, float]]] = {}
        self.statistics_calls = 0
        self.fail_statistics = False

    async def get_entity_state(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append((domain, service, data or {}))

    async def get_statistics(
        self,
        statistic_ids: list[str],
        start: datetime,
        end: datetime | None = None,
        period: str = "hour",
        types: tuple[str, ...] = ("mean", "min", "max"),
    ) -> dict[str, list[dict[str, Any]]]:
        self.statistics_calls += 1
        if self.fail_statistics:
            raise RuntimeError("statistics unavailable")
        return {
            sid: [
                r for r in self.statistics[sid]
                if r["start"] >= start and (end is None or r["start"] < end)
            ]
            for sid in statistic_ids
            if sid in self.statistics
        }

    async def get_weather_forecast(
        self, entity_id: str, forecast_type: str = "hourly"
    ) -> list[tuple[datetime, float]]:
        return list(self.weather.get(entity_id, []))

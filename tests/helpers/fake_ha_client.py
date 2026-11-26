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
        self.last_units: dict[str, str] | None = None
        # Result of get_energy_prefs (None → raise, like an unreachable HA).
        self.energy_prefs: dict[str, Any] | None = None
        # entity_id -> last_reported timestamp returned by get_entity
        self.reported: dict[str, datetime] = {}
        # entity_id -> attributes returned by get_entity
        self.attributes: dict[str, dict[str, Any]] = {}
        # service calls change the state like Home Assistant would
        self.apply_services = False

    async def get_entity_state(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    async def get_entity(self, entity_id: str):
        """``EntityState`` of *entity_id*; ``reported`` holds per-entity timestamps."""
        from nornwatt.plugins._homeassistant.client import EntityState

        if entity_id not in self._states:
            return None
        ts = self.reported.get(entity_id)
        return EntityState(
            state=None if self._states[entity_id] is None else str(self._states[entity_id]),
            attributes=dict(self.attributes.get(entity_id, {})), last_changed=ts, last_updated=ts, last_reported=ts,
        )

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append((domain, service, data or {}))
        if self.apply_services:
            self._apply_service(service, data or {})

    def set_state(self, entity_id: str, state: Any, **attributes: Any) -> None:
        self._states[entity_id] = state
        if attributes:
            self.attributes[entity_id] = attributes

    def _apply_service(self, service: str, data: dict[str, Any]) -> None:
        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        if service in ("turn_on", "turn_off"):
            self._states[entity_id] = "on" if service == "turn_on" else "off"
        elif service == "set_value":
            self._states[entity_id] = str(data["value"])
        elif service == "select_option":
            self._states[entity_id] = str(data["option"])
        elif service == "set_hvac_mode":
            self._states[entity_id] = str(data["hvac_mode"])
        else:  # climate / water heater services that set an attribute
            key = {"set_temperature": "temperature", "set_preset_mode": "preset_mode",
                   "set_operation_mode": "operation_mode"}.get(service)
            if key is not None:
                self.attributes.setdefault(entity_id, {})[key] = data[key]

    async def get_statistics(
        self,
        statistic_ids: list[str],
        start: datetime,
        end: datetime | None = None,
        period: str = "hour",
        types: tuple[str, ...] = ("mean", "min", "max"),
        units: dict[str, str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        self.statistics_calls += 1
        self.last_units = units
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

    async def get_energy_prefs(self) -> dict[str, Any]:
        if self.energy_prefs is None:
            raise RuntimeError("energy prefs unavailable")
        return self.energy_prefs

    async def get_weather_forecast(
        self, entity_id: str, forecast_type: str = "hourly"
    ) -> list[tuple[datetime, float]]:
        return list(self.weather.get(entity_id, []))

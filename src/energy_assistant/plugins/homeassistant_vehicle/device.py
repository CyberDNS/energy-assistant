"""HAVehicleDevice — an electric car whose data comes from Home Assistant."""

from __future__ import annotations

import logging
import time
from typing import Any

from ...core.device_model import Capability, DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.client import EntityState

_log = logging.getLogger(__name__)

_ON = {"on", "true", "home", "connected", "plugged"}
# Unit of a distance sensor → factor to km.
_TO_KM = {"km": 1.0, "m": 0.001, "mi": 1.609344}


def _float(entity: EntityState | None) -> float | None:
    if entity is None or entity.state is None:
        return None
    try:
        return float(entity.state)
    except (TypeError, ValueError):
        return None  # "unavailable", "unknown", …


def _bool(entity: EntityState | None) -> bool | None:
    if entity is None or entity.state in (None, "unavailable", "unknown"):
        return None
    return str(entity.state).lower() in _ON


def _km(entity: EntityState | None) -> float | None:
    value = _float(entity)
    if value is None or entity is None:
        return None
    unit = str(entity.attributes.get("unit_of_measurement") or "km").lower()
    return value * _TO_KM.get(unit, 1.0)


class HAVehicleDevice:
    """A ``Device`` of category ``vehicle`` read from Home Assistant entities.

    Reads are cached for *poll_interval_s*: car integrations update every
    15–60 min, there is no point in asking Home Assistant every poll.
    """

    def __init__(
        self,
        device_id: str,
        client: Any,
        *,
        entities: dict[Capability, str],
        refresh_entity: str | None = None,
        poll_interval_s: float = 30.0,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._entities = dict(entities)
        self._refresh_entity = refresh_entity
        self._poll_interval_s = poll_interval_s
        self._cached: DeviceState | None = None
        self._cached_at = 0.0

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.VEHICLE

    @property
    def can_refresh(self) -> bool:
        return self._refresh_entity is not None

    async def _read(self, capability: Capability) -> EntityState | None:
        entity_id = self._entities.get(capability)
        if entity_id is None:
            return None
        try:
            return await self._client.get_entity(entity_id)
        except Exception as exc:  # noqa: BLE001 — one bad entity must not hide the others
            _log.debug("Vehicle %r: reading %s failed: %s", self._device_id, entity_id, exc)
            return None

    async def get_state(self) -> DeviceState:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._poll_interval_s:
            return self._cached
        soc = await self._read(Capability.SOC_PCT)
        soc_pct = _float(soc)
        extra: dict[str, Any] = {
            "soc_timestamp": (
                soc.last_reported.isoformat()
                if soc is not None and soc.last_reported is not None and soc_pct is not None
                else None
            ),
        }
        if Capability.RANGE_KM in self._entities:
            extra["range_km"] = _km(await self._read(Capability.RANGE_KM))
        if Capability.PLUGGED in self._entities:
            extra["car_plugged"] = _bool(await self._read(Capability.PLUGGED))
        if Capability.LOCATION_HOME in self._entities:
            extra["location_home"] = _bool(await self._read(Capability.LOCATION_HOME))
        state = DeviceState(
            device_id=self._device_id,
            soc_pct=soc_pct,
            available=soc_pct is not None,
            extra=extra,
        )
        self._cached, self._cached_at = state, now
        return state

    async def refresh(self) -> None:
        """Ask the car integration for fresh data (presses the refresh button)."""
        if self._refresh_entity is None:
            return
        domain = self._refresh_entity.split(".", 1)[0]
        service = "press" if domain in ("button", "input_button") else "turn_on"
        await self._client.call_service(domain, service, {"entity_id": self._refresh_entity})
        self._cached = None  # read again on the next poll

    async def send_command(self, command: DeviceCommand) -> None:
        """A vehicle is charged through its charger; it takes no commands."""

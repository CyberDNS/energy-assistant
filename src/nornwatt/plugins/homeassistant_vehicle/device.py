"""HAVehicleDevice — an electric car whose data comes from Home Assistant."""

from __future__ import annotations

import time
from typing import Any

from ...core.device_model import Capability, DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.bindings import ReadBinding, Reading


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
        bindings: dict[Capability, ReadBinding],
        refresh_entity: str | None = None,
        poll_interval_s: float = 30.0,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._bindings = dict(bindings)
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

    async def _read(self, capability: Capability) -> Reading | None:
        binding = self._bindings.get(capability)
        return await binding.read(self._client) if binding is not None else None

    async def get_state(self) -> DeviceState:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._poll_interval_s:
            return self._cached
        soc = await self._read(Capability.SOC_PCT)
        soc_pct = soc.value if soc is not None else None
        extra: dict[str, Any] = {
            "soc_timestamp": (
                soc.reported_at.isoformat()
                if soc is not None and soc.reported_at is not None and soc_pct is not None
                else None
            ),
        }
        for cap, key in ((Capability.RANGE_KM, "range_km"),
                         (Capability.PLUGGED, "car_plugged"),
                         (Capability.LOCATION_HOME, "location_home")):
            if cap in self._bindings:
                reading = await self._read(cap)
                extra[key] = reading.value if reading is not None else None
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

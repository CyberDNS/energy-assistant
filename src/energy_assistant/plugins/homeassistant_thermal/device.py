"""ThermalLoadDevice — a thermal load read from HA, switched by its switch device.

State reported
--------------
``DeviceState.power_w``
    The switch's power (its power sensor, or its rated power while on).

``DeviceState.extra["measured_value"]``
    Current reading of the value entity (temperature, humidity, …).

``DeviceState.extra["switch_state"]``
    The switch's raw state ("on" / "off" / "unavailable").

Command handling
----------------
``set_power_w`` is passed on to the switch (> 0 → on, 0 → off).
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.client import HAClientProtocol

_log = logging.getLogger(__name__)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ThermalLoadDevice:
    """Thermal load: value entity in HA + the switch device that drives it."""

    def __init__(
        self,
        device_id: str,
        client: HAClientProtocol,
        entity_sensor: str,
        switch: Any,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._entity_sensor = entity_sensor
        self._switch = switch

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.THERMAL_LOAD

    @property
    def switch_id(self) -> str:
        return str(self._switch.device_id)

    async def get_state(self) -> DeviceState:
        try:
            measured = _to_float(await self._client.get_entity_state(self._entity_sensor))
        except Exception:  # noqa: BLE001 — no reading: reported as unavailable
            _log.warning("ThermalLoadDevice %r: failed to read %s from Home Assistant",
                         self._device_id, self._entity_sensor, exc_info=True)
            measured = None
        switch = await self._switch.get_state()
        return DeviceState(
            device_id=self._device_id,
            power_w=switch.power_w,
            available=measured is not None and switch.available,
            extra={
                "measured_value": measured,
                "switch_state": switch.extra.get("switch_state"),
            },
        )

    async def send_command(self, command: DeviceCommand) -> None:
        await self._switch.send_command(command)

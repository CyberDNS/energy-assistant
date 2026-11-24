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

from typing import Any

from ...core.device_model import DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.bindings import ReadBinding


class ThermalLoadDevice:
    """Thermal load: value entity in HA + the switch device that drives it."""

    def __init__(
        self,
        device_id: str,
        client: Any,
        sensor: ReadBinding,
        switch: Any,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._sensor = sensor
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
        measured = (await self._sensor.read(self._client)).value
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

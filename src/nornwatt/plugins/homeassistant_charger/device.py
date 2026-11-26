"""HAChargerDevice — an EV charger read and commanded through Home Assistant.

State reported
--------------
``power_w``             the ``power_w`` binding (charging power)
``available``           a car is plugged in (the ``plugged`` binding)
``extra["plugged"]``    the same, for the charging session logic
``extra["actuators"]``  whether each written entity follows

Commands
--------
``set_power_w`` → ``ChargerControl.apply`` with ``charge``, ``absorb_surplus``
or ``stop`` (``core.models.charger_behaviour``).  ``update_target_soc`` sets
the value of the ``soc_limit`` number; ``release()`` the release mode.
"""

from __future__ import annotations

from typing import Any

from ...core.device_model import Capability, DeviceCategory
from ...core.models import Behaviour, DeviceCommand, DeviceState, charger_behaviour
from .._homeassistant.bindings import ReadBinding
from .control import ChargerControl


class HAChargerDevice:
    def __init__(
        self,
        device_id: str,
        client: Any,
        readings: dict[Capability, ReadBinding],
        control: ChargerControl | None,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._readings = dict(readings)
        self._control = control

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.CHARGER

    @property
    def control(self) -> ChargerControl | None:
        return self._control

    @property
    def supported_behaviours(self) -> frozenset[Behaviour] | None:
        return self._control.supported_behaviours if self._control is not None else None

    async def get_state(self) -> DeviceState:
        reading = self._readings.get(Capability.POWER_W)
        power = (await reading.read(self._client)).value if reading is not None else None
        plug = self._readings.get(Capability.PLUGGED)
        # Without a plug sensor (old openwb_homeassistant configs) a car is
        # taken as always connected.
        plugged = (await plug.read(self._client)).value if plug is not None else True
        extra: dict[str, Any] = {"plugged": plugged is True}
        if self._control is not None:
            extra["actuators"] = [a.health.to_dict() for a in self._control.actuators]
        return DeviceState(
            device_id=self._device_id,
            power_w=power,
            available=plugged is True,
            extra=extra,
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """Raises when a write fails; the control loop logs it and retries."""
        if command.command != "set_power_w" or self._control is None:
            return
        value = float(command.value) if command.value is not None else 0.0
        await self._control.apply(value, charger_behaviour(command))

    def update_target_soc(self, soc_pct: float | None) -> None:
        if self._control is not None:
            self._control.update_target_soc(soc_pct)

    async def release(self) -> None:
        if self._control is not None:
            await self._control.release()

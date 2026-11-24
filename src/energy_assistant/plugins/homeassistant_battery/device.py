"""HABatteryDevice — a battery read and commanded through Home Assistant.

State reported
--------------
``power_w``          the ``power_w`` binding (+ = charging)
``soc_pct``          the ``soc_pct`` binding
``available``        both readings are there
``extra["actuators"]``  whether each written entity follows (``ActuatorHealth``)

Commands
--------
``set_power_w`` with its ``behaviour`` → ``BatteryControl.apply``.
``release()`` (start, reload, stop of Energy Assistant) → the release mode.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability, DeviceCategory
from ...core.models import Behaviour, DeviceCommand, DeviceState
from .._homeassistant.bindings import ReadBinding
from .control import BatteryControl

_log = logging.getLogger(__name__)


class HABatteryDevice:
    def __init__(
        self,
        device_id: str,
        client: Any,
        readings: dict[Capability, ReadBinding],
        control: BatteryControl | None,
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
        return DeviceCategory.BATTERY

    @property
    def control(self) -> BatteryControl | None:
        return self._control

    @property
    def supported_behaviours(self) -> frozenset[Behaviour] | None:
        return self._control.supported_behaviours if self._control is not None else None

    async def get_state(self) -> DeviceState:
        power = (await self._readings[Capability.POWER_W].read(self._client)).value
        soc = (await self._readings[Capability.SOC_PCT].read(self._client)).value
        extra: dict[str, Any] = {}
        if self._control is not None:
            extra["actuators"] = [a.health.to_dict() for a in self._control.actuators]
        return DeviceState(
            device_id=self._device_id,
            power_w=power,
            soc_pct=soc,
            available=power is not None and soc is not None,
            extra=extra,
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """``set_power_w`` (W, + charge / − discharge) with its behaviour.
        Raises when a write fails; the control loop logs it and retries."""
        if command.command != "set_power_w" or self._control is None:
            return
        await self._control.apply(float(command.value or 0.0), command.behaviour)

    async def release(self) -> None:
        if self._control is not None:
            await self._control.release()

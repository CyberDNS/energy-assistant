"""HAThermostatDevice — a thermal load that is its own switch (a thermostat).

State reported
--------------
``extra["measured_value"]``  the temperature (``temperature_c`` binding)
``power_w``                  the ``power_w`` binding; without one, the rated
                             power while the thermostat runs (its
                             ``hvac_action`` is heating / cooling / …, or — for
                             an entity without it — while it was told to run)
``extra["running"]``         whether it runs, as used for ``power_w``
``extra["hvac_action"]``     the raw hvac_action, if the entity has one
``available``                the temperature is there

Commands
--------
``set_power_w`` > 0 → run, 0 → stop (``ThermostatControl``).  When no command
came for ``stale_after_min``, the next state read restores the normal
operation; so does ``release()`` (start, reload and stop of Energy Assistant).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ...core.device_model import Capability, DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.bindings import ReadBinding
from .control import ThermostatControl

_log = logging.getLogger(__name__)


class HAThermostatDevice:
    def __init__(
        self,
        device_id: str,
        client: Any,
        readings: dict[Capability, ReadBinding],
        control: ThermostatControl | None,
        rated_power_w: float,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._readings = dict(readings)
        self._control = control
        self._rated_w = rated_power_w
        self._commanded_run: bool | None = None
        self._last_command_at: float | None = None
        self._restored = False
        self._clock = time.monotonic

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.THERMAL_LOAD

    @property
    def control(self) -> ThermostatControl | None:
        return self._control

    async def _hvac_action(self) -> str | None:
        if self._control is None:
            return None
        try:
            entity = await self._client.get_entity(self._control.entity_id)
        except Exception:  # noqa: BLE001 — unknown: fall back to the command
            return None
        action = entity.attributes.get("hvac_action") if entity is not None else None
        return str(action).lower() if action is not None else None

    async def get_state(self) -> DeviceState:
        await self._restore_if_stale()
        measured = (await self._readings[Capability.TEMPERATURE_C].read(self._client)).value
        action = await self._hvac_action()
        if action is not None and self._control is not None:
            running = action in self._control.running_states
        else:
            running = bool(self._commanded_run)
        power: float | None = None
        if Capability.POWER_W in self._readings:
            power = (await self._readings[Capability.POWER_W].read(self._client)).value
        if power is None:
            power = self._rated_w if running else 0.0
        extra: dict[str, Any] = {"measured_value": measured, "running": running}
        if action is not None:
            extra["hvac_action"] = action
        if self._control is not None:
            extra["actuators"] = [a.health.to_dict() for a in self._control.actuators]
        return DeviceState(device_id=self._device_id, power_w=power,
                           available=measured is not None, extra=extra)

    async def send_command(self, command: DeviceCommand) -> None:
        """``set_power_w`` > 0 runs, 0 stops.  Raises when the write fails."""
        if command.command != "set_power_w" or self._control is None:
            return
        run = command.value is not None and float(command.value) > 0
        self._last_command_at = self._clock()
        self._restored = False
        await self._control.apply(run)
        self._commanded_run = run

    async def _restore_if_stale(self) -> None:
        c = self._control
        if (c is None or c.normal is None or self._restored or self._last_command_at is None
                or self._clock() - self._last_command_at < c.stale_after_s):
            return
        self._restored = True
        self._commanded_run = None
        try:
            await c.restore()
            _log.warning("%r: no command for %.0f min — restored the normal operation (%r)",
                         self._device_id, c.stale_after_s / 60, c.normal)
        except Exception:  # noqa: BLE001 — tried again after the next command goes stale
            _log.warning("%r: restoring the normal operation failed", self._device_id,
                         exc_info=True)

    async def release(self) -> None:
        if self._control is not None:
            await self._control.restore()
            self._commanded_run = None

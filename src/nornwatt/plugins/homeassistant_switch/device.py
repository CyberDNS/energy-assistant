"""HASwitchDevice — an on/off switch in Home Assistant.

State reported
--------------
``DeviceState.power_w``
    The bound power sensor, else ``rated_power_w`` while on and 0 while off
    (None when neither is known).

``DeviceState.energy_kwh``
    The bound energy counter, if any.

``DeviceState.extra["switch_state"]``
    Raw HA state of the switch entity ("on" / "off" / "unavailable").

``DeviceState.extra["actuator"]``
    Whether the switch follows what was written (``ActuatorHealth``).

Command handling
----------------
``set_power_w``
    value > 0 → on, value == 0 → off, through the shared ``SwitchActuator``:
    only written on a mismatch (so the control tick doesn't spam
    turn_on/turn_off, and a plug toggled by hand is put back), rate-limited,
    and reported when the switch does not follow.

Release
-------
``release()`` (start, reload and stop of Nornwatt, stale plan, stuck
control loop) puts the switch to its ``release_state``: ``on`` (default —
the load's own thermostat or timer takes over), ``off`` or ``leave``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ...core.device_model import DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.actuators import SwitchActuator
from .._homeassistant.bindings import ReadBinding

_log = logging.getLogger(__name__)

_GONE = {"unavailable", "unknown", "none", ""}


class HASwitchDevice:
    """Switch (plug, relay) controlled through Home Assistant."""

    def __init__(
        self,
        device_id: str,
        client: Any,
        switch: SwitchActuator,
        power: ReadBinding | None = None,
        energy: ReadBinding | None = None,
        rated_power_w: float | None = None,
        release_state: str = "on",
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._switch = switch
        self._power = power
        self._energy = energy
        self._rated_power_w = rated_power_w
        self._release_state = release_state

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.SWITCH

    @property
    def entity_switch(self) -> str:
        return self._switch.entity_id

    async def get_state(self) -> DeviceState:
        try:
            entity = await self._client.get_entity(self._switch.entity_id)
        except Exception:  # noqa: BLE001 — reported as unavailable
            _log.warning("HASwitchDevice %r: failed to read state from Home Assistant",
                         self._device_id, exc_info=True)
            return DeviceState(device_id=self._device_id, available=False)
        raw = str(entity.state) if entity is not None and entity.state is not None else "unavailable"
        on = raw.lower() == "on"
        power_w: float | None = None
        if self._power is not None:
            power_w = (await self._power.read(self._client)).value
        if power_w is None and self._rated_power_w is not None:
            power_w = self._rated_power_w if on else 0.0
        energy = (await self._energy.read(self._client)).value if self._energy else None
        return DeviceState(
            device_id=self._device_id,
            power_w=power_w,
            energy_kwh=energy,
            available=raw.lower() not in _GONE,
            extra={"switch_state": raw, "on": on,
                   "actuator": self._switch.health.to_dict()},
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """Switch on (``set_power_w`` > 0) or off (0)."""
        if command.command != "set_power_w":
            return
        want_on = command.value is not None and float(command.value) > 0
        with contextlib.suppress(Exception):  # logged by the actuator, retried next tick
            await self._switch.apply(want_on)

    async def release(self) -> None:
        """Put the switch to its ``release_state`` (``leave``: unchanged)."""
        if self._release_state == "leave":
            return
        want_on = self._release_state == "on"
        # Through the actuator: no write when it already is in that state.
        if await self._switch.apply(want_on):
            _log.info("HASwitchDevice %r: released (%s)", self._device_id, self._release_state)

"""HASwitchDevice — an on/off switch in Home Assistant.

State reported
--------------
``DeviceState.power_w``
    The bound power sensor, else ``rated_power_w`` while on and 0 while off
    (None when neither is known).

``DeviceState.extra["switch_state"]``
    Raw HA state of the switch entity ("on" / "off" / "unavailable").

Command handling
----------------
``set_power_w``
    value > 0 → ``homeassistant.turn_on``, value == 0 → ``homeassistant.turn_off``.
"""

from __future__ import annotations

import logging

from ...core.device_model import DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.client import HAClientProtocol

_log = logging.getLogger(__name__)

_GONE = {"unavailable", "unknown", "none"}


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class HASwitchDevice:
    """Switch (plug, relay) controlled through Home Assistant."""

    def __init__(
        self,
        device_id: str,
        client: HAClientProtocol,
        entity_switch: str,
        entity_power: str | None = None,
        rated_power_w: float | None = None,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._entity_switch = entity_switch
        self._entity_power = entity_power
        self._rated_power_w = rated_power_w

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.SWITCH

    @property
    def entity_switch(self) -> str:
        return self._entity_switch

    async def get_state(self) -> DeviceState:
        try:
            raw = str(await self._client.get_entity_state(self._entity_switch))
            on = raw.lower() == "on"
            power_w: float | None = None
            if self._entity_power is not None:
                power_w = _to_float(await self._client.get_entity_state(self._entity_power))
            if power_w is None and self._rated_power_w is not None:
                power_w = self._rated_power_w if on else 0.0
            return DeviceState(
                device_id=self._device_id,
                power_w=power_w,
                available=raw.lower() not in _GONE,
                extra={"switch_state": raw, "on": on},
            )
        except Exception:  # noqa: BLE001 — reported as unavailable
            _log.warning("HASwitchDevice %r: failed to read state from Home Assistant",
                         self._device_id, exc_info=True)
            return DeviceState(device_id=self._device_id, available=False)

    async def send_command(self, command: DeviceCommand) -> None:
        """Switch on (``set_power_w`` > 0) or off (0).

        Reconciles first: reads the switch and only calls the HA service on a
        mismatch, so the control tick doesn't spam turn_on/turn_off — and an
        actual correction (e.g. someone toggled the plug by hand) shows in
        the log at INFO level.  If the read fails the command is sent anyway.
        """
        if command.command != "set_power_w":
            return
        want_on = command.value is not None and float(command.value) > 0

        current: str | None = None
        try:
            raw = await self._client.get_entity_state(self._entity_switch)
            current = str(raw).lower() if raw is not None else None
        except Exception as exc:  # noqa: BLE001 — read failed: write unconditionally
            _log.debug("HASwitchDevice %r: reading %s failed: %s", self._device_id,
                       self._entity_switch, exc)

        if current == ("on" if want_on else "off"):
            return

        try:
            service = "turn_on" if want_on else "turn_off"
            await self._client.call_service(
                "homeassistant", service, {"entity_id": self._entity_switch}
            )
            _log.info("HASwitchDevice %r: switch %s → %s (was %r)", self._device_id,
                      self._entity_switch, "on" if want_on else "off", current)
        except Exception:  # noqa: BLE001 — retried on the next control tick
            _log.warning("HASwitchDevice %r: failed to send command to %s",
                         self._device_id, self._entity_switch, exc_info=True)

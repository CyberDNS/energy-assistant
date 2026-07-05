"""ThresholdHADevice — reads a sensor and controls an on/off switch via HA.

Implements the ``Device`` protocol for threshold-controlled loads such as
aquarium coolers, dehumidifiers, or space heaters.

State reported
--------------
``DeviceState.power_w``
    Rated power when the switch is on; 0.0 when off.

``DeviceState.extra["measured_value"]``
    Current reading of the sensor entity (temperature, humidity, …).

``DeviceState.extra["switch_state"]``
    Raw HA state string of the switch entity ("on" / "off" / "unavailable").

Command handling
----------------
``set_power_w``
    value > 0  → ``homeassistant.turn_on`` the switch entity.
    value == 0 → ``homeassistant.turn_off`` the switch entity.
"""

from __future__ import annotations

import logging

from ...core.models import DeviceCommand, DeviceRole, DeviceState
from .._homeassistant.client import HAClientProtocol

_log = logging.getLogger(__name__)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ThresholdHADevice:
    """Device that reads a sensor and controls a switch in Home Assistant.

    Parameters
    ----------
    device_id:
        Stable identifier for this device.
    client:
        An open HA client.
    entity_sensor:
        HA entity ID for the measured value (e.g. temperature, humidity).
    entity_switch:
        HA entity ID for the on/off switch.
    rated_power_w:
        Electrical power drawn when the switch is on (W).
    """

    def __init__(
        self,
        device_id: str,
        client: HAClientProtocol,
        entity_sensor: str,
        entity_switch: str,
        rated_power_w: float,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._entity_sensor = entity_sensor
        self._entity_switch = entity_switch
        self._rated_power_w = rated_power_w

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return DeviceRole.THRESHOLD_CONTROLLED

    async def get_state(self) -> DeviceState:
        """Read sensor and switch state from Home Assistant."""
        try:
            sensor_raw = await self._client.get_entity_state(self._entity_sensor)
            measured = _to_float(sensor_raw)

            switch_raw = await self._client.get_entity_state(self._entity_switch)
            switch_on = str(switch_raw).lower() == "on"
            power_w = self._rated_power_w if switch_on else 0.0

            available = measured is not None and str(switch_raw).lower() != "unavailable"

            return DeviceState(
                device_id=self._device_id,
                power_w=power_w,
                available=available,
                extra={
                    "measured_value": measured,
                    "switch_state": str(switch_raw),
                },
            )

        except Exception:
            _log.warning(
                "ThresholdHADevice %r: failed to read state from Home Assistant",
                self._device_id,
                exc_info=True,
            )
            return DeviceState(device_id=self._device_id, available=False)

    async def send_command(self, command: DeviceCommand) -> None:
        """Turn the switch on or off based on the ``set_power_w`` value."""
        if command.command != "set_power_w":
            return
        try:
            if command.value is not None and float(command.value) > 0:
                _log.debug("ThresholdHADevice %r: turning ON %s", self._device_id, self._entity_switch)
                await self._client.call_service(
                    "homeassistant", "turn_on", {"entity_id": self._entity_switch}
                )
            else:
                _log.debug("ThresholdHADevice %r: turning OFF %s", self._device_id, self._entity_switch)
                await self._client.call_service(
                    "homeassistant", "turn_off", {"entity_id": self._entity_switch}
                )
        except Exception:
            _log.warning(
                "ThresholdHADevice %r: failed to send command to %s",
                self._device_id,
                self._entity_switch,
                exc_info=True,
            )

"""OpenWBDevice — reads openWB chargepoint state from HA, writes charging mode.

Charging mode is controlled by calling the HA ``select.select_option`` service
on the ``entity_mode`` entity.  The mode is determined by interpreting the
``set_power_w`` command value as a sentinel:

  value > 500 W  → mode_instant  ("Instant Charging")
  0 < value ≤ 500 W  → mode_pv    ("PV Charging")
  value == 0.0   → mode_stop      ("Stop")

See ``assets/ev.py`` for the full encoding contract.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import DeviceCommand, DeviceRole, DeviceState
from .._homeassistant.client import HAClientProtocol

_log = logging.getLogger(__name__)

_MODE_THRESHOLD_W = 500.0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return None if f != f else f  # guard NaN
    except (TypeError, ValueError):
        return None


class OpenWBDevice:
    """Device plugin for an openWB EV chargepoint.

    Parameters
    ----------
    device_id:
        Stable identifier for this chargepoint.
    client:
        Home Assistant REST client.
    entity_mode:
        HA select entity that controls the charging mode,
        e.g. ``select.openwbmaster_chargepoint_5_lademodus``.
    entity_soc:
        HA sensor returning the vehicle's SoC in % (0–100),
        e.g. ``sensor.openwbmaster_chargepoint_5_ladung``.
    entity_power:
        Optional HA sensor returning current charging power in W,
        e.g. ``sensor.openwbmaster_chargepoint_5_w``.
    entity_plugged:
        Optional HA binary sensor for cable connection status,
        e.g. ``binary_sensor.openwbmaster_chargepoint_5_ladekabel``.
        When omitted the device is assumed to be always available.
    mode_pv, mode_instant, mode_stop:
        Exact option strings as they appear in the HA select entity.
    """

    def __init__(
        self,
        device_id: str,
        client: HAClientProtocol,
        *,
        entity_mode: str,
        entity_soc: str,
        entity_power: str | None = None,
        entity_plugged: str | None = None,
        entity_soc_limit_instant: str | None = None,
        mode_pv: str = "PV Charging",
        mode_instant: str = "Instant Charging",
        mode_stop: str = "Stop",
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._entity_mode = entity_mode
        self._entity_soc = entity_soc
        self._entity_power = entity_power
        self._entity_plugged = entity_plugged
        self._entity_soc_limit_instant = entity_soc_limit_instant
        self._mode_pv = mode_pv
        self._mode_instant = mode_instant
        self._mode_stop = mode_stop
        self._target_soc_pct: float | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return DeviceRole.EV_CHARGER

    def update_target_soc(self, soc_pct: float | None) -> None:
        """Store the active goal's target SoC so it can be written to the
        instant-charging SoC limit entity when instant mode is activated."""
        self._target_soc_pct = soc_pct

    async def get_state(self) -> DeviceState:
        """Read SoC, power, and cable status from Home Assistant."""
        try:
            soc_pct: float | None = None
            power_w: float | None = None
            plugged = True  # default to available when no plug sensor configured

            soc_raw = await self._client.get_entity_state(self._entity_soc)
            soc_pct = _to_float(soc_raw)

            if self._entity_power:
                pwr_raw = await self._client.get_entity_state(self._entity_power)
                power_w = _to_float(pwr_raw)

            if self._entity_plugged:
                plug_raw = await self._client.get_entity_state(self._entity_plugged)
                plugged = str(plug_raw).lower() in ("on", "true", "1", "connected")

            available = plugged and soc_pct is not None
            return DeviceState(
                device_id=self._device_id,
                power_w=power_w,
                soc_pct=soc_pct,
                available=available,
                extra={"plugged": plugged},
            )

        except Exception:
            _log.warning(
                "OpenWBDevice %r: failed to read state from HA", self._device_id,
                exc_info=True,
            )
            return DeviceState(device_id=self._device_id, available=False)

    async def send_command(self, command: DeviceCommand) -> None:
        """Translate a ``set_power_w`` sentinel into an openWB mode selection."""
        if command.command != "set_power_w":
            return

        value = float(command.value) if command.value is not None else 0.0
        if value > _MODE_THRESHOLD_W:
            mode = self._mode_instant
        elif value > 0.0:
            mode = self._mode_pv
        else:
            mode = self._mode_stop

        try:
            await self._client.call_service(
                "select",
                "select_option",
                {"entity_id": self._entity_mode, "option": mode},
            )
            _log.debug(
                "OpenWBDevice %r: set mode %r (value=%.0f W)",
                self._device_id, mode, value,
            )
        except Exception:
            _log.warning(
                "OpenWBDevice %r: failed to set mode %r", self._device_id, mode,
                exc_info=True,
            )

        # When switching to instant charging, also write the SoC limit so
        # openWB stops at the planned target SoC.
        if mode == self._mode_instant and self._entity_soc_limit_instant and self._target_soc_pct is not None:
            try:
                await self._client.call_service(
                    "number",
                    "set_value",
                    {"entity_id": self._entity_soc_limit_instant, "value": round(self._target_soc_pct)},
                )
                _log.debug(
                    "OpenWBDevice %r: set instant SoC limit to %.0f%%",
                    self._device_id, self._target_soc_pct,
                )
            except Exception:
                _log.warning(
                    "OpenWBDevice %r: failed to set instant SoC limit", self._device_id,
                    exc_info=True,
                )

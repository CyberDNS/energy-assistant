"""DifferentialDevice — derives power as ``minuend.power_w − subtrahend.power_w``.

This is the core implementation for **Messkonzept 8** (Wärmepumpentarif)
setups where heat-pump consumption is not directly metered but can be
derived from two existing meters:

    heatpump_power = main_grid_import − household_import

Messkonzept 8 wiring
---------------------
In Messkonzept 8 (common in Germany, Luxembourg, and Austria for heat-pump
households on a special Wärmepumpentarif):

- **Main grid meter** (Z1) sits at the grid connection point.  It measures
  total electricity drawn from the grid by *both* the household circuit and
  the heat-pump circuit.  When configured with ``oid_power_import``, the
  ``DeviceState.extra["import_w"]`` field holds gross import (always ≥ 0).
  Use ``minuend_field = "extra.import_w"`` to derive the heat-pump load
  correctly even when PV is exporting.

- **Household meter** (Z2) sits on the household sub-circuit only (not the
  heat-pump leg).  Its ``power_w`` is always ≥ 0.

- **Heat pump** (derived) = Z1 import − Z2 import.  Set ``min_power_w = 0.0``
  to avoid negative results during transient reading differences.

Example config
--------------
::

    devices:
      main_grid_meter:
        role: meter
        source:
          type: generic_iobroker
          power_import: "tibberlink.0.Homes.<id>.LiveMeasurement.powerImport"
          power_export: "tibberlink.0.Homes.<id>.LiveMeasurement.powerExport"
        tariff: export_tariff

      household_meter:
        role: meter
        source:
          type: generic_iobroker
          power: "<household-sub-circuit-oid>"
        tariff: household_tariff

      heatpump:
        role: consumer
        source:
          type: differential
          minuend: main_grid_meter
          minuend_field: "extra.import_w"   # use gross import, not net
          subtrahend: household_meter
          min_w: 0.0
        tariff: heatpump_tariff

Field access
------------
``minuend_field`` and ``subtrahend_field`` support two formats:

``"power_w"``
    Use ``DeviceState.power_w`` (the default).
``"extra.<key>"``
    Use ``DeviceState.extra["<key>"]``, e.g. ``"extra.import_w"``.
"""

from __future__ import annotations

import logging

from ...core.device import Device
from ...core.models import DeviceCommand, DeviceRole, DeviceState

_log = logging.getLogger(__name__)


def _read_field(state: DeviceState, field: str) -> float | None:
    """Return the value of *field* from *state*.

    Supported formats: ``"power_w"`` or ``"extra.<key>"``.
    Returns ``None`` when the field is absent or not numeric.
    """
    if field == "power_w":
        return state.power_w
    if field.startswith("extra."):
        key = field[6:]
        value = state.extra.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    _log.warning("DifferentialDevice: unknown field %r", field)
    return None


class DifferentialDevice:
    """A read-only ``Device`` whose power is derived as ``minuend − subtrahend``.

    Implements the ``Device`` protocol structurally (no inheritance required).

    Parameters
    ----------
    device_id:
        Stable, unique identifier for this device.
    role:
        Semantic role (typically ``DeviceRole.CONSUMER``).
    minuend:
        The device whose reading is subtracted *from*.
    subtrahend:
        The device whose reading is *subtracted*.
    minuend_field:
        Which field of the minuend ``DeviceState`` to use.
        Defaults to ``"power_w"``.  Use ``"extra.import_w"`` for a
        bidirectional grid meter in Messkonzept 8.
    subtrahend_field:
        Which field of the subtrahend ``DeviceState`` to use.
        Defaults to ``"power_w"``.
    min_power_w:
        Clamp the result to at least this value (e.g. ``0.0`` for a
        consumer that cannot produce energy).
    max_power_w:
        Clamp the result to at most this value.
    """

    def __init__(
        self,
        device_id: str,
        role: DeviceRole,
        minuend: Device,
        subtrahend: Device,
        *,
        minuend_field: str = "power_w",
        subtrahend_field: str = "power_w",
        min_power_w: float | None = None,
        max_power_w: float | None = None,
    ) -> None:
        self._device_id = device_id
        self._role = role
        self._minuend = minuend
        self._subtrahend = subtrahend
        self._minuend_field = minuend_field
        self._subtrahend_field = subtrahend_field
        self._min_power_w = min_power_w
        self._max_power_w = max_power_w

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

    async def get_state(self) -> DeviceState:
        """Derive power by reading both source devices and computing the difference."""
        m_state = await self._minuend.get_state()
        s_state = await self._subtrahend.get_state()

        m_val = _read_field(m_state, self._minuend_field)
        s_val = _read_field(s_state, self._subtrahend_field)

        power_w: float | None = None
        if m_val is not None and s_val is not None:
            power_w = m_val - s_val
            if self._min_power_w is not None:
                power_w = max(power_w, self._min_power_w)
            if self._max_power_w is not None:
                power_w = min(power_w, self._max_power_w)

        return DeviceState(
            device_id=self._device_id,
            power_w=power_w,
            available=m_state.available and s_state.available,
            extra={
                "minuend_value": m_val,
                "subtrahend_value": s_val,
            },
        )

    async def send_command(self, command: DeviceCommand) -> None:
        # Derived consumer with no direct control — silently ignored.
        pass

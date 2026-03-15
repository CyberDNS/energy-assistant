"""
SMA battery device backed by the ioBroker *modbus* adapter (instance 0).

The ioBroker Modbus adapter reads SMA Sunny Boy Storage / SMA Home Storage
registers via Modbus TCP and publishes them under:

    modbus.0.inputRegisters.<register>_0

The register numbers used here follow the *SMA Modbus Interface Definition*
(document SMA-Modbus-general-TI-en-22, or device-specific variants).

Typical OIDs (as published by the ioBroker Modbus adapter)
----------------------------------------------------------
- ``modbus.0.inputRegisters.30775_PowerAC``   Net AC power of the inverter (W)
                                              Negative = charging, positive = discharging.
                                              This is the primary power measurement.
- ``modbus.0.inputRegisters.30845_BAT_SoC``   Battery SoC (%)
- ``modbus.0.holdingRegisters.40189_WMaxCha`` Max charge power (W) — configured in inverter
- ``modbus.0.holdingRegisters.40191_WMaxDsch``Max discharge power (W) — configured in inverter

These defaults can be replaced via the ``oid_*`` constructor parameters to
accommodate different SMA models or custom Modbus mappings.

This battery is **not controllable** by the MILP optimizer — it is managed
automatically by the SMA inverter.  The device therefore does not declare
``storage_constraints`` and the optimizer will treat its output as part of the
measured household state rather than a scheduling variable.

Usage::

    from energy_manager.plugins.sma_modbus_iobroker.device import SMAModbusIoBrokerDevice

    device = SMAModbusIoBrokerDevice(
        device_id="sma_battery",
        client=client,
    )
    state = await device.get_state()
    print(state.soc_pct, state.power_w)
"""

from __future__ import annotations

from datetime import datetime, timezone

from .._iobroker.client import IoBrokerClientProtocol
from ...core.models import DeviceCategory, DeviceState

# Default SMA Modbus register OIDs for the ioBroker modbus adapter (instance 0).
# The suffix after the register number is the label assigned in ioBroker.
_DEFAULT_OID_POWER_W = "modbus.0.inputRegisters.30775_PowerAC"   # signed net W; negative = charging
_DEFAULT_OID_SOC = "modbus.0.inputRegisters.30845_BAT_SoC"
_DEFAULT_OID_MAX_CHARGE_W = "modbus.0.holdingRegisters.40189_WMaxCha"
_DEFAULT_OID_MAX_DISCHARGE_W = "modbus.0.holdingRegisters.40191_WMaxDsch"


class SMAModbusIoBrokerDevice:
    """
    Reads SMA battery state from ioBroker's Modbus adapter.

    This device is **read-only** — it exposes no ``storage_constraints`` and
    cannot be scheduled by the MILP optimizer.  The SMA inverter controls the
    battery autonomously.

    Parameters
    ----------
    device_id:
        Stable identifier used throughout the platform (e.g. ``"sma_battery"``).
    client:
        An open ``IoBrokerClient``.
    oid_power_w:
        ioBroker object ID for signed net AC power (W).
        Negative = charging, positive = discharging.
        Default: ``modbus.0.inputRegisters.30775_PowerAC``
    oid_soc:
        ioBroker object ID for battery state of charge (%).
        Default: ``modbus.0.inputRegisters.30845_BAT_SoC``
    oid_max_charge_w:
        ioBroker object ID for max charge power (W).
        Default: ``modbus.0.holdingRegisters.40189_WMaxCha``
    oid_max_discharge_w:
        ioBroker object ID for max discharge power (W).
        Default: ``modbus.0.holdingRegisters.40191_WMaxDsch``
    """

    def __init__(
        self,
        device_id: str,
        client: IoBrokerClientProtocol,
        *,
        oid_power_w: str = _DEFAULT_OID_POWER_W,
        oid_soc: str = _DEFAULT_OID_SOC,
        oid_max_charge_w: str = _DEFAULT_OID_MAX_CHARGE_W,
        oid_max_discharge_w: str = _DEFAULT_OID_MAX_DISCHARGE_W,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._oid_power_w = oid_power_w
        self._oid_soc = oid_soc
        self._oid_max_charge_w = oid_max_charge_w
        self._oid_max_discharge_w = oid_max_discharge_w

    # ------------------------------------------------------------------
    # Device protocol
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.STORAGE

    @property
    def storage_constraints(self) -> None:
        """
        Not controllable — the SMA inverter manages this battery autonomously.
        Always returns ``None`` so the MILP optimizer ignores it.
        """
        return None

    async def get_state(self) -> DeviceState:
        """
        Read current battery state from ioBroker Modbus registers.

        ``power_w`` convention:
        - **Positive** = discharging (delivering energy to home/grid)
        - **Negative** = charging (consuming energy)
        """
        oids = [
            self._oid_power_w,
            self._oid_soc,
            self._oid_max_charge_w,
            self._oid_max_discharge_w,
        ]
        raw = await self._client.get_bulk(oids)

        def _float(oid: str) -> float | None:
            val = raw.get(oid)
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        # PowerAC sign convention matches our platform: negative = charging, positive = discharging
        net_power_w = _float(self._oid_power_w)

        # WMaxCha/WMaxDsch: 0xFFFFFFFF means "no limit set" — treat as None.
        _SMA_NaN = 4294967295
        raw_max_cha = _float(self._oid_max_charge_w)
        raw_max_dsch = _float(self._oid_max_discharge_w)
        max_charge_w = raw_max_cha if (raw_max_cha is not None and raw_max_cha != _SMA_NaN) else None
        max_discharge_w = raw_max_dsch if (raw_max_dsch is not None and raw_max_dsch != _SMA_NaN) else None

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=net_power_w,
            soc_pct=_float(self._oid_soc),
            available=True,
            extra={
                "category": DeviceCategory.STORAGE.value,
                "controllable": False,
                "max_charge_w": max_charge_w,
                "max_discharge_w": max_discharge_w,
            },
        )

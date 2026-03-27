"""SmaSunnyBoyStorageDevice — SMA Sunny Boy Storage battery via ioBroker Modbus adapter.

Control mechanism
-----------------
The SBS is controlled exclusively through a single Modbus holding register:

    40016_WirkleistungBeg  — Active power limitation, 0–100 %

Setting this to X % allows the inverter to export (discharge) up to X % of its
rated power to the home / grid.  Setting it to 0 % prevents all discharge.

The command ``set_power_w`` translates the desired discharge power (negative
platform sign = producing) to a percentage via::

    pct = abs(power_w) / (max_discharge_w × V_max/V_nominal) × 100

The ``V_max/V_nominal`` factor (default 253/230) corrects for the higher
real-power capability at the EU upper voltage limit (253 V).  This matches
the ioBroker script that originally drove this device.

Key Modbus OIDs
---------------
Input registers (read-only):

    30775_PowerAC      — AC power at device connection point (W).
                         SMA sign convention: positive = exporting/discharging.
                         Mapped to platform sign: power_w = −PowerAC.
    30845_BAT_SoC      — Battery state of charge (%).

Holding registers (read/write):

    40016_WirkleistungBeg  — Discharge power limit (write, 0–100 %).
    40189_WMaxCha          — Maximum charge power on the inverter (W, read).
    40191_WMaxDsch         — Maximum discharge power on the inverter (W, read).

Sign convention
---------------
Consistent with the rest of the platform:

``power_w > 0``  — charging (consuming energy from grid/PV)
``power_w < 0``  — discharging (producing energy for home/grid)
``power_w == 0`` — idle
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...core.models import DeviceCommand, DeviceRole, DeviceState, StorageConstraints
from .._iobroker.client import IoBrokerClientProtocol

_log = logging.getLogger(__name__)


class SmaSunnyBoyStorageDevice:
    """Controllable storage device backed by the ioBroker Modbus adapter for SMA SBS.

    The only writable control point is ``WirkleistungBeg`` (register 40016):
    a discharge power limit expressed as a percentage (0–100 %) of rated power.
    Charge power is managed automatically by the inverter (PV excess / grid surplus).

    Parameters
    ----------
    device_id:
        Stable unique identifier (e.g. ``"sma_battery"``).
    client:
        An open ioBroker client (e.g. from ``IoBrokerConnectionPool``).
    modbus_instance:
        ioBroker Modbus adapter instance prefix (e.g. ``"modbus.0"``).
    capacity_kwh:
        Usable battery capacity in kWh.
    max_charge_kw:
        Maximum charge power in kW.
    max_discharge_kw:
        Maximum discharge power in kW.  Used in the percentage calculation
        when the live ``WMaxDsch`` register cannot be read.
    voltage_max_v:
        Maximum grid voltage for the power-percentage correction.
        Defaults to 253 V (EU upper regulatory limit).
    voltage_nominal_v:
        Nominal grid voltage.  Defaults to 230 V.
    """

    def __init__(
        self,
        device_id: str,
        client: IoBrokerClientProtocol,
        modbus_instance: str = "modbus.0",
        *,
        capacity_kwh: float,
        max_charge_kw: float,
        max_discharge_kw: float,
        voltage_max_v: float = 253.0,
        voltage_nominal_v: float = 230.0,
        purchase_price_eur: float | None = None,
        cycle_life: int | None = None,
        no_grid_charge: bool = False,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._mb = modbus_instance
        self._capacity_kwh = capacity_kwh
        self._max_charge_kw = max_charge_kw
        self._max_discharge_kw = max_discharge_kw
        self._purchase_price_eur = purchase_price_eur
        self._cycle_life = cycle_life
        self._no_grid_charge = no_grid_charge
        # Pre-computed voltage correction factor (253/230 ≈ 1.1)
        self._v_factor = voltage_max_v / voltage_nominal_v

    # --- OID helpers ---------------------------------------------------------

    def _hr(self, reg: str) -> str:
        """Build a holding-register OID."""
        return f"{self._mb}.holdingRegisters.{reg}"

    def _ir(self, reg: str) -> str:
        """Build an input-register OID."""
        return f"{self._mb}.inputRegisters.{reg}"

    # --- Device protocol -----------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return DeviceRole.STORAGE

    @property
    def storage_constraints(self) -> StorageConstraints:
        return StorageConstraints(
            device_id=self._device_id,
            capacity_kwh=self._capacity_kwh,
            max_charge_kw=self._max_charge_kw,
            max_discharge_kw=self._max_discharge_kw,
            purchase_price_eur=self._purchase_price_eur,
            cycle_life=self._cycle_life,
            no_grid_charge=self._no_grid_charge,
        )

    async def get_state(self) -> DeviceState:
        """Read battery state from ioBroker Modbus.

        ``power_w > 0`` = charging (consuming); ``power_w < 0`` = discharging (producing).
        """
        oids = {
            "power_ac":   self._ir("30775_PowerAC"),
            "soc":        self._ir("30845_BAT_SoC"),
            "max_dsch_w": self._hr("40191_WMaxDsch"),
            "max_cha_w":  self._hr("40189_WMaxCha"),
            "limit_pct":  self._hr("40016_WirkleistungBeg"),
        }
        try:
            raw = await self._client.get_bulk(list(oids.values()))
        except Exception:
            _log.warning(
                "SmaSunnyBoyStorageDevice %r: failed to read state", self._device_id
            )
            return DeviceState(device_id=self._device_id, available=False)

        def _f(key: str) -> float | None:
            val = raw.get(oids[key])
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        power_ac = _f("power_ac")
        # SMA PowerAC: positive = discharging/exporting → platform sign: negative
        power_w = (-power_ac) if power_ac is not None else None

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=power_w,
            soc_pct=_f("soc"),
            available=True,
            extra={
                "discharge_limit_pct": _f("limit_pct"),
                "max_dsch_w":          _f("max_dsch_w"),
                "max_cha_w":           _f("max_cha_w"),
            },
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """Send a control command to the SMA Sunny Boy Storage.

        Supported commands
        ------------------
        ``set_power_w``
            Target net power in W (platform sign convention):
            positive = charge, negative = discharge, 0 = idle / no export.

            Discharge (negative value): translated to a ``WirkleistungBeg``
            percentage and written via Modbus.

            Charge or idle (non-negative value): ``WirkleistungBeg`` is set
            to 0 % so the battery will not export to the grid.  The inverter
            manages charging from PV / grid automatically.
        """
        if command.command != "set_power_w":
            raise NotImplementedError(
                f"SmaSunnyBoyStorageDevice does not support command {command.command!r}"
            )

        power_w = float(command.value)

        if power_w >= 0:
            # No discharge requested — prevent grid export.
            pct = 0
        else:
            # Translate desired discharge power to a percentage.
            # Use the configured max discharge power as the rated reference;
            # apply voltage correction factor (default 253/230).
            max_dsch_w = self._max_discharge_kw * 1000.0
            pct = int(round(
                min(100.0, max(0.0, abs(power_w) / (max_dsch_w * self._v_factor) * 100.0))
            ))

        await self._client.set_value(self._hr("40016_WirkleistungBeg"), pct)
        _log.debug(
            "SmaSunnyBoyStorageDevice %r: WirkleistungBeg = %d %% (requested %.0f W)",
            self._device_id,
            pct,
            power_w,
        )

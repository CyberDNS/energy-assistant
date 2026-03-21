"""ZendureIoBrokerDevice — Zendure SolarFlow battery via ioBroker zendure-solarflow adapter.

OID prefix
----------
All OIDs live under:

    zendure-solarflow.0.<hub_id>.<device_serial>.*

Key read OIDs
-------------
``electricLevel``     State of charge (%)
``outputPackPower``   Power flowing INTO the battery pack (W, i.e. charging)
``packInputPower``    Power flowing OUT OF the battery pack (W, i.e. discharging)
``solarInputPower``   Solar PV power entering the hub (W)
``outputHomePower``   Power delivered to home from hub (W)
``minSoc``            Min SoC threshold configured on device (%)
``socSet``            Max SoC threshold configured on device (%)

Key control OIDs
----------------
``control.acMode``
    1 = charge from grid/AC, 2 = discharge to home.

``control.setInputLimit``
    Charge power limit in W (grid → battery).  Set alongside ``acMode = 1``.

``control.setOutputLimit``
    Discharge power limit in W (battery → home).  Set alongside ``acMode = 2``.

Sign convention
---------------
Consistent with the rest of the platform:

``power_w > 0``  — charging (consuming energy from grid/PV)
``power_w < 0``  — discharging (producing energy for home)
``power_w == 0`` — idle

This is the **opposite** of the old energy_manager convention.  The sign
flip is applied in ``get_state()``; control commands in ``send_command()``
translate accordingly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...core.models import DeviceCommand, DeviceRole, DeviceState, StorageConstraints
from .._iobroker.client import IoBrokerClientProtocol

_log = logging.getLogger(__name__)


class ZendureIoBrokerDevice:
    """A controllable ``Device`` backed by the ioBroker zendure-solarflow adapter.

    Implements the ``Device`` protocol structurally (no inheritance).

    Parameters
    ----------
    device_id:
        Stable, unique identifier (e.g. ``"zendure"``).
    client:
        An open ioBroker client (e.g. from ``IoBrokerConnectionPool``).
    hub_id:
        The Zendure hub ID in the adapter OID tree (e.g. ``"gDa3tb"``).
    device_serial:
        The device serial in the OID tree (e.g. ``"B1613x21"``).
    capacity_kwh:
        Usable battery capacity in kWh.
    max_charge_kw:
        Maximum charge power in kW.
    max_discharge_kw:
        Maximum discharge power in kW.
    maintenance_charge_w:
        Charge power (W) applied when SoC drops below ~5% to protect the cells.
        Default 300 W.
    """

    def __init__(
        self,
        device_id: str,
        client: IoBrokerClientProtocol,
        hub_id: str,
        device_serial: str,
        *,
        capacity_kwh: float,
        max_charge_kw: float,
        max_discharge_kw: float,
        maintenance_charge_w: float = 300.0,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._prefix = f"zendure-solarflow.0.{hub_id}.{device_serial}"
        self._capacity_kwh = capacity_kwh
        self._max_charge_kw = max_charge_kw
        self._max_discharge_kw = max_discharge_kw
        self._maintenance_charge_w = maintenance_charge_w

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
        )

    @property
    def maintenance_charge_w(self) -> float:
        return self._maintenance_charge_w

    async def get_state(self) -> DeviceState:
        """Read battery state from ioBroker.

        ``power_w > 0`` = charging (consuming); ``power_w < 0`` = discharging (producing).
        """
        p = self._prefix
        oid_map = {
            "soc":       f"{p}.electricLevel",
            "charge_w":  f"{p}.outputPackPower",   # power INTO pack = charging
            "disch_w":   f"{p}.packInputPower",    # power OUT OF pack = discharging
            "solar_w":   f"{p}.solarInputPower",
            "home_w":    f"{p}.outputHomePower",
            "min_soc":   f"{p}.minSoc",
            "max_soc":   f"{p}.socSet",
        }
        try:
            raw = await self._client.get_bulk(list(oid_map.values()))
        except Exception:
            _log.warning("ZendureIoBrokerDevice %r: failed to read state", self._device_id)
            return DeviceState(device_id=self._device_id, available=False)

        def _f(key: str) -> float | None:
            val = raw.get(oid_map[key])
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        charge_w = _f("charge_w") or 0.0
        disch_w  = _f("disch_w")  or 0.0

        # Positive = charging (consuming), negative = discharging (producing)
        power_w = charge_w - disch_w

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=power_w,
            soc_pct=_f("soc"),
            available=True,
            extra={
                "charge_w":    charge_w,
                "discharge_w": disch_w,
                "solar_w":     _f("solar_w"),
                "home_w":      _f("home_w"),
                "min_soc_pct": _f("min_soc"),
                "max_soc_pct": _f("max_soc"),
            },
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """Send a control command to the Zendure via ioBroker.

        Supported commands
        ------------------
        ``set_power_w``
            Target net power in W using platform sign convention:
            positive = charge, negative = discharge, 0 = idle.

        ``set_charge_limit``
            Upper SoC limit in percent (0–100).

        ``set_discharge_limit``
            Lower SoC limit in percent (0–100).
        """
        p = self._prefix

        if command.command == "set_power_w":
            power_w = float(command.value)
            if power_w > 0:   # charge
                await self._client.set_value(f"{p}.control.acMode", 1)
                await self._client.set_value(f"{p}.control.setInputLimit", int(power_w))
                await self._client.set_value(f"{p}.control.setOutputLimit", 0)
            elif power_w < 0:  # discharge
                await self._client.set_value(f"{p}.control.acMode", 2)
                await self._client.set_value(f"{p}.control.setInputLimit", 0)
                await self._client.set_value(f"{p}.control.setOutputLimit", int(-power_w))
            else:              # idle
                await self._client.set_value(f"{p}.control.setInputLimit", 0)
                await self._client.set_value(f"{p}.control.setOutputLimit", 0)

        elif command.command == "set_charge_limit":
            await self._client.set_value(f"{p}.control.chargeLimit", int(command.value))

        elif command.command == "set_discharge_limit":
            await self._client.set_value(f"{p}.control.dischargeLimit", int(command.value))

        else:
            raise NotImplementedError(
                f"ZendureIoBrokerDevice does not support command {command.command!r}"
            )

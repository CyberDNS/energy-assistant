"""
Zendure SolarFlow battery device backed by the ioBroker *zendure-solarflow* adapter.

The adapter exposes battery state under:

    zendure-solarflow.0.{hub_id}.{device_serial}.*

Key read OIDs
-------------
- ``electricLevel``     State of charge (%)
- ``outputPackPower``   Charge power flowing into battery pack (W)
- ``packInputPower``    Discharge power flowing out of battery pack (W)
- ``solarInputPower``   Solar PV power entering the hub (W)
- ``outputHomePower``   Power delivered to home from hub (W)
- ``minSoc``            Min SoC threshold set on device (%)
- ``socSet``            Max SoC threshold set on device (%)

Key control OIDs
----------------
- ``control.setDeviceAutomationInOutLimit``
    Signed power setpoint in **Watts**.
    Negative = charge battery from grid/solar.
    Positive = discharge battery / feed into home.

    This is the primary output of the MILP optimizer.

- ``control.chargeLimit``    Upper SoC % limit (0–100)
- ``control.dischargeLimit`` Lower SoC % limit (0–100)

Usage::

    from energy_manager.plugins._iobroker.pool import IoBrokerConnectionPool
    from energy_manager.plugins.zendure_iobroker.device import ZendureIoBrokerDevice

    pool = IoBrokerConnectionPool()
    device = ZendureIoBrokerDevice(
        device_id="zendure",
        client=pool.get("192.168.2.30"),
        hub_id="gDa3tb",
        device_serial="B1613x21",
    )
    state = await device.get_state()
    print(state.soc_pct, state.power_w)
"""

from __future__ import annotations

from datetime import datetime, timezone

from .._iobroker.client import IoBrokerClientProtocol
from ...core.models import DeviceCategory, DeviceCommand, DeviceState, StorageConstraints


class ZendureIoBrokerDevice:
    """
    Reads Zendure SolarFlow battery state from ioBroker and sends power
    setpoints back via the *setDeviceAutomationInOutLimit* control OID.

    Parameters
    ----------
    device_id:
        Stable identifier used throughout the platform (e.g. ``"zendure"``).
    client:
        An open ``IoBrokerClient``.
    hub_id:
        The Zendure hub ID in the adapter OID tree (e.g. ``"gDa3tb"``).
    device_serial:
        The Zendure device serial in the adapter OID tree (e.g. ``"B1613x21"``).
    capacity_kwh:
        Usable battery capacity in kWh.  When supplied together with
        ``max_charge_kw`` and ``max_discharge_kw``, the device declares itself
        controllable via the ``storage_constraints`` property.
    max_charge_kw:
        Maximum charge power in kW.
    max_discharge_kw:
        Maximum discharge power in kW.
    maintenance_charge_w:
        Charge power (W) applied when the battery enters maintenance mode
        (SoC drops below ~5 %).  Default 300 W.
    """

    def __init__(
        self,
        device_id: str,
        client: IoBrokerClientProtocol,
        hub_id: str,
        device_serial: str,
        *,
        capacity_kwh: float | None = None,
        max_charge_kw: float | None = None,
        max_discharge_kw: float | None = None,
        maintenance_charge_w: float = 300.0,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._prefix = f"zendure-solarflow.0.{hub_id}.{device_serial}"
        self._capacity_kwh = capacity_kwh
        self._max_charge_kw = max_charge_kw
        self._max_discharge_kw = max_discharge_kw
        self._maintenance_charge_w = maintenance_charge_w

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
    def maintenance_charge_w(self) -> float:
        """Charge power (W) to apply when the battery is in maintenance mode."""
        return self._maintenance_charge_w

    @property
    def storage_constraints(self) -> StorageConstraints | None:
        """
        Declare this device as a controllable storage unit.

        Returns ``None`` when capacity configuration is absent — the device
        will then be ignored by the MILP optimizer.
        """
        if (
            self._capacity_kwh is None
            or self._max_charge_kw is None
            or self._max_discharge_kw is None
        ):
            return None
        return StorageConstraints(
            device_id=self._device_id,
            capacity_kwh=self._capacity_kwh,
            max_charge_kw=self._max_charge_kw,
            max_discharge_kw=self._max_discharge_kw,
        )

    async def get_state(self) -> DeviceState:
        """
        Read current battery state from ioBroker.

        ``power_w`` follows the convention:
        - **Positive** = discharging (producing energy for home/grid)
        - **Negative** = charging (consuming energy)
        """
        p = self._prefix
        oids = {
            "soc": f"{p}.electricLevel",
            "charge_w": f"{p}.outputPackPower",
            "discharge_w": f"{p}.packInputPower",
            "solar_w": f"{p}.solarInputPower",
            "home_out_w": f"{p}.outputHomePower",
            "min_soc": f"{p}.minSoc",
            "max_soc": f"{p}.socSet",
        }
        raw = await self._client.get_bulk(list(oids.values()))

        def _float(key: str) -> float | None:
            val = raw.get(oids[key])
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        charge_w = _float("charge_w") or 0.0
        discharge_w = _float("discharge_w") or 0.0
        # Positive = discharging (producing), negative = charging (consuming)
        net_power_w = discharge_w - charge_w

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=net_power_w,
            soc_pct=_float("soc"),
            available=True,
            extra={
                "category": DeviceCategory.STORAGE.value,
                "solar_input_w": _float("solar_w"),
                "home_output_w": _float("home_out_w"),
                "min_soc_pct": _float("min_soc"),
                "max_soc_pct": _float("max_soc"),
            },
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """
        Send a control command to the Zendure device via ioBroker.

        Supported commands
        ------------------
        ``set_automation_limit``
            Set ``control.setDeviceAutomationInOutLimit`` in **Watts**.
            Negative = charge, Positive = discharge / feed in.
            This is the primary MILP optimizer output.

        ``set_charge_limit``
            Set ``control.chargeLimit`` in percent (0–100).

        ``set_discharge_limit``
            Set ``control.dischargeLimit`` in percent (0–100).
        """
        p = self._prefix
        if command.command == "set_automation_limit":
            oid = f"{p}.control.setDeviceAutomationInOutLimit"
        elif command.command == "set_charge_limit":
            oid = f"{p}.control.chargeLimit"
        elif command.command == "set_discharge_limit":
            oid = f"{p}.control.dischargeLimit"
        else:
            raise NotImplementedError(
                f"ZendureIoBrokerDevice does not support command '{command.command}'"
            )
        await self._client.set_value(oid, command.value)

    async def set_power_w(self, power_w: float) -> None:
        """
        Send a real-time power setpoint to the Zendure device.

        Uses the three-OID control interface confirmed by the production
        ioBroker JavaScript controller:

        - ``control.acMode``       1 = charge mode, 2 = discharge mode
        - ``control.setInputLimit``  charge power in W (grid → battery)
        - ``control.setOutputLimit`` discharge power in W (battery → home)

        Sign convention matches the rest of the platform:

        - ``power_w < 0`` → charge from grid / PV
        - ``power_w > 0`` → discharge into home
        - ``power_w == 0`` → idle (both limits cleared)
        """
        p = self._prefix
        if power_w < 0:  # charging
            await self._client.set_value(f"{p}.control.acMode", 1)
            await self._client.set_value(f"{p}.control.setInputLimit", int(-power_w))
            await self._client.set_value(f"{p}.control.setOutputLimit", 0)
        elif power_w > 0:  # discharging
            await self._client.set_value(f"{p}.control.acMode", 2)
            await self._client.set_value(f"{p}.control.setInputLimit", 0)
            await self._client.set_value(f"{p}.control.setOutputLimit", int(power_w))
        else:  # idle
            await self._client.set_value(f"{p}.control.setInputLimit", 0)
            await self._client.set_value(f"{p}.control.setOutputLimit", 0)

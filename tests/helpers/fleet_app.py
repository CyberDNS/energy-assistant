"""An Application with one vehicle at one charger, for session tests."""

from __future__ import annotations

from nornwatt.assets.fleet import VehicleFleet
from nornwatt.core.device_model import parse_devices
from nornwatt.core.registry import DeviceRegistry
from nornwatt.server import Application


class FakeSessionStorage:
    async def open_vehicle_session(self, *_a) -> int:
        return 1

    async def close_vehicle_sessions(self, *_a) -> None:
        pass

    async def clear_ev_force_charge(self, *_a) -> None:
        pass

    async def save_next_vehicles(self, choices) -> None:
        self.next_vehicles = dict(choices)

    async def save_guest_modes(self, modes) -> None:
        self.guest_modes = dict(modes)


def fleet_app(vehicle: dict | None = None, charger: dict | None = None) -> Application:
    """An Application with one vehicle ``ev1`` whose default charger is ``wallbox``."""
    devices = parse_devices({
        "wallbox": {
            "category": "charger", "type": "openwb_mqtt", "chargepoint_id": 1,
            "params": {"default_vehicle": "ev1", "max_power_kw": 11.0, **(charger or {})},
        },
        "ev1": {
            "category": "vehicle", "type": "homeassistant_vehicle", "label": "EV",
            "capabilities": {"soc_pct": {"entity": "sensor.ev1_soc"}},
            "params": {"capacity_kwh": 60.0, "timezone": "Europe/Berlin", **(vehicle or {})},
        },
    })
    app = Application()
    app._fleet = VehicleFleet(devices)
    app._ev_assets = []
    app._ev_force_charge = {}
    app._disabled_chargepoints = set()
    app._ev_plugged_since = {}
    app._ev_overdue_dismissed = {}
    app._storage = FakeSessionStorage()  # type: ignore[assignment]
    app._registry = DeviceRegistry()
    app._dry_run = True

    async def no_plan() -> None:
        pass

    app._run_plan = no_plan  # type: ignore[method-assign]
    app._apply_sessions()
    return app

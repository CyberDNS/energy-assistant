"""Vehicles as devices: sessions, SoC estimation, the fleet, the HA vehicle plugin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from helpers.fake_ha_client import FakeHAClient
from helpers.fleet_app import fleet_app

from nornwatt.assets.fleet import VehicleFleet
from nornwatt.core.config import BackendsConfig
from nornwatt.core.device_model import (
    Capability,
    DeviceConfigError,
    parse_devices,
)
from nornwatt.core.models import DeviceState
from nornwatt.core.plugin_registry import BuildContext
from nornwatt.core.vehicles import (
    GUEST,
    GUEST_INSTANT,
    MANUAL,
    PRESELECTED,
    ChargingSession,
    SessionChange,
    SessionManager,
    SocEstimator,
    efficiency_at,
)
from nornwatt.loader.device_loader import validate_plugins
from nornwatt.plugins import registry as plugin_registry
from nornwatt.storage.sqlite import SqliteStorageBackend

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


# ── SoC estimation ───────────────────────────────────────────────────────────


def test_efficiency_from_charge_curve() -> None:
    curve = [(80.0, 0.9), (100.0, 0.55)]
    assert efficiency_at(curve, 50.0) == 0.9
    assert efficiency_at(curve, 85.0) == 0.55
    assert efficiency_at(curve, 100.0) == 0.55
    assert efficiency_at([], 50.0) == 1.0


def test_estimate_adds_charger_energy_until_the_next_report() -> None:
    est = SocEstimator(capacity_kwh=50.0, curve=[(100.0, 0.9)])
    assert est.observe(40.0, T0)
    est.add_energy(5.0)                       # 5 kWh × 0.9 / 50 kWh = +9 %
    assert est.state.soc_pct == pytest.approx(49.0)
    assert est.state.estimated and est.state.reported_soc_pct == 40.0
    # The same report again does not reset the estimate …
    assert not est.observe(40.0, T0)
    assert est.state.soc_pct == pytest.approx(49.0)
    # … a newer one does, even with the same value.
    assert est.observe(48.0, T0 + timedelta(minutes=30))
    assert est.state.soc_pct == 48.0 and not est.state.estimated


def test_estimate_caps_at_100_and_ignores_missing_values() -> None:
    est = SocEstimator(capacity_kwh=10.0)
    est.add_energy(5.0)                       # no report yet → nothing to add to
    assert est.state.soc_pct is None
    est.observe(95.0, T0)
    est.add_energy(5.0)
    assert est.state.soc_pct == 100.0
    assert not est.observe(None, T0 + timedelta(hours=1))


def test_report_without_timestamp_resets_on_value_change() -> None:
    est = SocEstimator(capacity_kwh=50.0)
    est.observe(40.0, None)
    est.add_energy(5.0)
    assert not est.observe(40.0, None)        # unchanged value: keep the estimate
    assert est.observe(52.0, None)


# ── Sessions ─────────────────────────────────────────────────────────────────


def _sessions() -> SessionManager:
    return SessionManager(
        default_vehicles={"cp4": "scooter", "cp5": "bandit", "cp6": None},
        vehicles=frozenset({"bandit", "scooter"}),
    )


def test_plug_in_assigns_the_default_vehicle_and_unplug_ends_it() -> None:
    sm = _sessions()
    assert sm.update({"cp5": True}, T0) == [SessionChange("cp5", "bandit", True)]
    assert sm.charger_of("bandit") == "cp5"
    assert sm.update({"cp5": True}, T0) == []
    assert sm.update({"cp5": None}, T0) == []          # unknown: keep the session
    assert sm.update({"cp5": False}, T0) == [
        SessionChange("cp5", "bandit", False, unplugged=True)
    ]
    assert sm.charger_of("bandit") is None


def test_charger_without_default_starts_a_guest_session() -> None:
    sm = _sessions()
    assert sm.update({"cp6": True}, T0) == [SessionChange("cp6", None, True)]
    assert sm.sessions["cp6"].is_guest


def test_default_vehicle_already_elsewhere_makes_a_guest() -> None:
    sm = _sessions()
    sm.update({"cp4": True}, T0)
    sm.assign("cp4", "bandit", T0)                     # bandit charges at cp4
    assert sm.update({"cp5": True}, T0) == [SessionChange("cp5", None, True)]


def test_reassigning_moves_a_vehicle_and_leaves_a_guest_behind() -> None:
    sm = _sessions()
    sm.update({"cp4": True, "cp5": True}, T0)
    changes = sm.assign("cp4", "bandit", T0 + timedelta(minutes=5))
    assert changes == [
        SessionChange("cp5", "bandit", False), SessionChange("cp5", None, True),
        SessionChange("cp4", "scooter", False), SessionChange("cp4", "bandit", True),
    ]
    assert sm.charger_of("bandit") == "cp4" and sm.charger_of("scooter") is None
    assert sm.sessions["cp5"].is_guest and sm.sessions["cp4"].source == MANUAL
    assert sm.sessions["cp4"].started_at == T0          # still the plug-in time
    assert sm.assign("cp4", "bandit", T0) == []


def test_assign_errors() -> None:
    sm = _sessions()
    with pytest.raises(ValueError, match="no car is plugged in"):
        sm.assign("cp5", "bandit", T0)
    sm.update({"cp5": True}, T0)
    with pytest.raises(ValueError, match="unknown vehicle"):
        sm.assign("cp5", "tesla", T0)


def test_restore_drops_unknown_and_duplicate_vehicles() -> None:
    sm = _sessions()
    sm.restore([
        ChargingSession("cp5", "bandit", T0, MANUAL, 7),
        ChargingSession("cp4", "bandit", T0),          # bandit can't be twice
        ChargingSession("cp6", "tesla", T0),            # no longer configured
        ChargingSession("gone", "scooter", T0),        # charger removed
    ])
    assert sm.sessions["cp5"].vehicle_id == "bandit"
    assert sm.sessions["cp5"].session_id == 7
    assert sm.sessions["cp4"].is_guest and sm.sessions["cp6"].is_guest
    assert "gone" not in sm.sessions


# ── Fleet: composition ───────────────────────────────────────────────────────


def _states(plugged: bool, power_w: float, soc: float, reported: datetime) -> dict:
    return {
        "wallbox": DeviceState(device_id="wallbox", power_w=power_w, available=plugged,
                               extra={"plugged": plugged, "chargemode": "pv"}),
        "ev1": DeviceState(device_id="ev1", soc_pct=soc,
                           extra={"soc_timestamp": reported.isoformat(), "range_km": 210.0}),
    }


def test_compose_lays_the_vehicle_soc_over_its_charger() -> None:
    app = fleet_app({"capacity_kwh": 50.0, "charge_curve": [{"soc_pct": 100, "efficiency": 0.9}]})
    fleet = app._fleet
    states = _states(True, 0.0, 40.0, T0)
    assert fleet.compose(states, T0) == [SessionChange("wallbox", "ev1", True)]
    assert states["wallbox"].soc_pct == 40.0
    assert states["wallbox"].extra["vehicle_id"] == "ev1"
    assert states["wallbox"].extra["range_km"] == 210.0
    assert states["ev1"].extra["charger_id"] == "wallbox"

    # 11 kW for 60 s twice (gaps are capped at 60 s), then a 30 s step: 2.5 min
    for dt in (60, 120, 150):
        states = _states(True, 11_000.0, 40.0, T0)
        fleet.compose(states, T0 + timedelta(seconds=dt))
    added = 11.0 * 2.5 / 60 * 0.9 / 50.0 * 100
    assert states["wallbox"].soc_pct == pytest.approx(40.0 + added)
    assert states["wallbox"].extra["soc_estimated"] is True
    assert states["ev1"].soc_pct == pytest.approx(40.0 + added)

    # The car reports again: the estimate starts over from its value.
    states = _states(True, 0.0, 43.0, T0 + timedelta(minutes=10))
    fleet.compose(states, T0 + timedelta(minutes=10))
    assert states["wallbox"].soc_pct == 43.0


def test_compose_without_a_car_leaves_the_charger_soc_empty() -> None:
    app = fleet_app()
    states = _states(False, 0.0, 40.0, T0)
    assert app._fleet.compose(states, T0) == []
    assert states["wallbox"].soc_pct is None
    assert states["wallbox"].extra["vehicle_id"] is None
    assert states["ev1"].extra["charger_id"] is None
    assert states["ev1"].soc_pct == 40.0


def test_assets_and_contributors_follow_the_session() -> None:
    app = fleet_app(charger={"min_power_kw": 4.14, "max_power_kw": 11.0, "guest_mode": "instant"})
    fleet = app._fleet
    (asset,) = fleet.assets()
    assert asset.device_id == "" and asset.asset_id == "ev1"

    fleet.compose(_states(True, 0.0, 40.0, T0), T0)
    app._apply_sessions()
    (asset,) = app._ev_assets
    assert (asset.device_id, asset.min_charge_kw, asset.max_charge_kw) == ("wallbox", 4.14, 11.0)
    contrib = fleet.contributors["wallbox"]
    assert contrib.asset.asset_id == "ev1" and contrib._guest_mode is None

    fleet.sessions.assign("wallbox", None, T0)
    app._apply_sessions()
    assert app._ev_assets[0].device_id == ""
    assert contrib.asset.asset_id == "guest@wallbox" and contrib._guest_mode == "instant"


def test_charger_ids_and_soc_push_target() -> None:
    app = fleet_app({"charger_ids": {"openwb_mqtt": 1}, "push_soc_to_charger": True})
    fleet = app._fleet
    assert fleet.charger_vehicle_id("ev1", "wallbox") == 1
    assert fleet.charger_vehicle_id(None, "wallbox") == 0
    assert fleet.soc_push_target("ev1") == ("wallbox", 1)
    assert fleet_app({"charger_ids": {"openwb_mqtt": 1}})._fleet.soc_push_target("ev1") is None
    assert fleet_app()._fleet.charger_vehicle_id("ev1", "wallbox") is None


def test_previous_ids_is_rejected_with_a_hint() -> None:
    with pytest.raises(DeviceConfigError, match="previous_ids is no longer used"):
        fleet_app({"previous_ids": ["ev_bandit"]})


def test_empty_fleet() -> None:
    fleet = VehicleFleet({})
    assert fleet.assets() == [] and fleet.compose({}, T0) == []


# ── Server: sessions and charger systems ─────────────────────────────────────


class _Charger:
    def __init__(self) -> None:
        self.assigned: list = []
        self.pushed: list = []

    async def assign_vehicle(self, vid) -> None:
        self.assigned.append(vid)

    async def push_vehicle_soc(self, vid, soc, range_km, reported_at) -> None:
        self.pushed.append((vid, round(soc, 1), range_km, reported_at))


def _live_app(**vehicle):
    app = fleet_app({"charger_ids": {"openwb_mqtt": 1}, **vehicle})
    charger = _Charger()
    app._registry.get = lambda did: charger if did == "wallbox" else None  # type: ignore[method-assign]
    app._dry_run = False
    app._soc_pushed = {}
    app._refresh_pressed = {}
    return app, charger


async def test_manual_reassignment_updates_the_charger_system() -> None:
    app, charger = _live_app()
    states = _states(True, 0.0, 40.0, T0)
    await app._on_session_changes(app._fleet.compose(states, T0), replan=False, now=T0)
    assert charger.assigned == [1]          # openWB had no assignment cached → set it

    await app.select_charger(None, "wallbox")    # a guest takes the charger
    assert charger.assigned == [1, 0]       # guest → openWB's default vehicle
    charger.assigned_vehicle_id = 1         # stale cache right after the change
    await app.select_charger("ev1", "wallbox")
    assert charger.assigned == [1, 0, 1]    # a manual change is always passed on
    with pytest.raises(ValueError, match="unknown charger"):
        await app.select_charger(None, "nope")


async def test_soc_is_pushed_on_change_only() -> None:
    app, charger = _live_app(push_soc_to_charger=True)
    states = _states(False, 0.0, 40.0, T0)
    app._fleet.compose(states, T0)
    await app._sync_vehicles(states)
    await app._sync_vehicles(states)
    assert charger.pushed == [(1, 40.0, 210.0, T0)]


# ── Config ───────────────────────────────────────────────────────────────────


def test_vehicle_and_charger_schemas() -> None:
    def errors(devices: dict) -> list[str]:
        with pytest.raises(DeviceConfigError) as exc:
            parse_devices(devices)
        return exc.value.errors

    car = {"category": "vehicle", "type": "homeassistant_vehicle",
           "capabilities": {"soc_pct": {"entity": "sensor.soc"}}, "params": {"capacity_kwh": 50}}
    assert "device 'cp': params.default_vehicle 'tesla' is not a configured vehicle" in errors({
        "car": car,
        "cp": {"category": "charger", "type": "openwb_mqtt", "chargepoint_id": 1,
               "params": {"default_vehicle": "tesla"}},
    })
    assert errors({"car": {**car, "params": {"capacity_kwh": 50, "charge_curve": [
        {"soc_pct": 90, "efficiency": 1.4}]}}}) == [
        "device 'car': params.charge_curve [1] efficiency must be <= 1, got 1.4"
    ]
    assert "params.guest_mode must be one of pv_only, instant, off" in errors({
        "cp": {"category": "charger", "type": "openwb_mqtt", "chargepoint_id": 1,
               "params": {"guest_mode": "sometimes"}},
    })[0]
    # A vehicle needs a SoC source.
    with pytest.raises(DeviceConfigError, match="requires capability 'soc_pct'"):
        validate_plugins(parse_devices({"car": {**car, "capabilities": {}}}))
    validate_plugins(parse_devices({"car": car}))


# ── homeassistant_vehicle plugin ─────────────────────────────────────────────


def _vehicle_device(client: FakeHAClient):
    cfg = {
        "type": "homeassistant_vehicle", "category": "vehicle",
        "refresh_entity": "button.car_refresh", "poll_interval_s": 0,
        "capabilities": {
            "soc_pct": {"entity": "sensor.car_soc"},
            "range_km": {"entity": "sensor.car_range"},
            "plugged": {"entity": "binary_sensor.car_plugged"},
            "location_home": {"entity": "device_tracker.car"},
        },
    }
    return plugin_registry.build_device(
        "car", cfg, BuildContext(backends=BackendsConfig(), ha_client=client)
    )


async def test_vehicle_device_reads_home_assistant() -> None:
    client = FakeHAClient({
        "sensor.car_soc": "28", "sensor.car_range": "136",
        "binary_sensor.car_plugged": "on", "device_tracker.car": "home",
    })
    client.reported["sensor.car_soc"] = T0
    device = _vehicle_device(client)
    state = await device.get_state()
    assert state.soc_pct == 28.0 and state.available
    assert state.extra == {
        "soc_timestamp": T0.isoformat(), "range_km": 136.0, "car_plugged": True,
        "location_home": True,
    }
    await device.refresh()
    assert client.calls == [("button", "press", {"entity_id": "button.car_refresh"})]


async def test_vehicle_device_unavailable_soc() -> None:
    client = FakeHAClient({"sensor.car_soc": "unavailable"})
    state = await _vehicle_device(client).get_state()
    assert state.soc_pct is None and not state.available
    assert state.extra["range_km"] is None and state.extra["car_plugged"] is None


def test_vehicle_plugin_rejects_bad_bindings() -> None:
    with pytest.raises(DeviceConfigError, match="unknown key"):
        plugin_registry.build_device(
            "car", {"type": "homeassistant_vehicle", "category": "vehicle",
                    "capabilities": {"soc_pct": {"field": "x"}}},
            BuildContext(backends=BackendsConfig(), ha_client=FakeHAClient()),
        )
    assert Capability.SOC_PCT in plugin_registry.device_info("homeassistant_vehicle").bindable


# ── Storage ──────────────────────────────────────────────────────────────────


async def test_session_storage_and_id_migration(tmp_path) -> None:
    db = SqliteStorageBackend(tmp_path / "t.db")
    await db.start()
    try:
        await _check_storage(db)
    finally:
        await db.stop()


async def _check_storage(db) -> None:

    sid = await db.open_vehicle_session("cp5", "bandit", T0, T0, "default")
    await db.open_vehicle_session("cp4", None, T0, T0, "default")
    open_rows = await db.load_open_vehicle_sessions()
    assert {(r["charger_id"], r["vehicle_id"], r["id"]) for r in open_rows} == {
        ("cp5", "bandit", sid), ("cp4", None, sid + 1),
    }
    await db.close_vehicle_sessions("cp5", T0 + timedelta(hours=1))
    assert [r["charger_id"] for r in await db.load_open_vehicle_sessions()] == ["cp4"]
    rows = await db.query_vehicle_sessions(T0 - timedelta(hours=1), T0 + timedelta(hours=2))
    assert [(r["charger_id"], r["ended_at"] is not None) for r in rows] == [
        ("cp5", True), ("cp4", False),
    ]



# ── Pre-selection for the next plug-in ───────────────────────────────────────


def test_preselected_vehicle_is_used_by_the_next_plug_in_only() -> None:
    sm = _sessions()
    sm.set_next("cp5", "scooter")
    assert sm.update({"cp5": True}, T0) == [SessionChange("cp5", "scooter", True)]
    assert sm.sessions["cp5"].source == PRESELECTED
    assert sm.next_vehicle == {}                          # used up
    sm.update({"cp5": False}, T0)
    sm.update({"cp5": True}, T0)
    assert sm.sessions["cp5"].vehicle_id == "bandit"     # back to the default


def test_preselected_guest_and_reset_to_default() -> None:
    sm = _sessions()
    sm.set_next("cp5", GUEST)
    sm.set_next("cp5", None)                              # back to the default
    sm.update({"cp5": True}, T0)
    assert sm.sessions["cp5"].vehicle_id == "bandit"
    sm.set_next("cp4", GUEST)
    sm.update({"cp4": True}, T0)
    assert sm.sessions["cp4"].is_guest and sm.sessions["cp4"].source == PRESELECTED


def test_preselection_moves_a_vehicle_that_is_charging_elsewhere() -> None:
    sm = _sessions()
    sm.update({"cp5": True}, T0)                          # bandit at cp5
    sm.set_next("cp4", "bandit")
    changes = sm.update({"cp4": True}, T0)
    assert changes == [
        SessionChange("cp5", "bandit", False), SessionChange("cp5", None, True),
        SessionChange("cp4", "bandit", True),
    ]


def test_a_vehicle_is_expected_at_one_charger_only() -> None:
    sm = _sessions()
    sm.set_next("cp4", "bandit")
    sm.set_next("cp6", "bandit")
    assert sm.next_vehicle == {"cp6": "bandit"}


def test_set_next_errors() -> None:
    sm = _sessions()
    with pytest.raises(ValueError, match="unknown charger"):
        sm.set_next("nope", None)
    with pytest.raises(ValueError, match="unknown vehicle"):
        sm.set_next("cp5", "tesla")
    sm.update({"cp5": True}, T0)
    with pytest.raises(ValueError, match="reassign it instead"):
        sm.set_next("cp5", "scooter")


async def test_next_vehicle_is_stored_and_shown_on_the_charger(tmp_path) -> None:
    app, _charger = _live_app()
    await app.select_charger(None, "wallbox")    # free: the guest is expected next
    assert app._storage.next_vehicles == {"wallbox": GUEST}
    states = _states(False, 0.0, 40.0, T0)
    app._fleet.compose(states, T0)
    assert states["wallbox"].extra["next_vehicle"] == GUEST
    # The plug-in uses it up, and storage follows.
    states = _states(True, 0.0, 40.0, T0)
    await app._on_session_changes(app._fleet.compose(states, T0), replan=False, now=T0)
    assert app._fleet.session("wallbox").is_guest
    assert app._storage.next_vehicles == {}

    db = SqliteStorageBackend(tmp_path / "n.db")
    await db.start()
    try:
        await db.save_next_vehicles({"cp4": "scooter"})
        assert await db.load_next_vehicles() == {"cp4": "scooter"}
        await db.save_next_vehicles({})
        assert await db.load_next_vehicles() == {}
    finally:
        await db.stop()



# ── Per car: select a charger (the last selection wins) ─────────────────────


def test_select_on_a_free_charger_waits_for_the_plug_in_then_the_default_returns() -> None:
    sm = _sessions()
    assert sm.status("bandit") == {"charger_id": "cp5", "state": "default"}
    sm.select(None, "cp5", T0)                       # a guest will use bandit's charger
    assert sm.status("bandit") == {"charger_id": "cp5", "state": "taken", "by": GUEST, "plugged": False}
    assert sm.guest_status() == [{"charger_id": "cp5", "state": "selected"}]
    sm.update({"cp5": True}, T0)
    assert sm.sessions["cp5"].is_guest
    assert sm.status("bandit")["state"] == "taken" and sm.status("bandit")["plugged"] is True
    sm.update({"cp5": False}, T0)                    # the guest leaves
    assert sm.status("bandit") == {"charger_id": "cp5", "state": "default"}
    sm.update({"cp5": True}, T0)
    assert sm.sessions["cp5"].vehicle_id == "bandit"


def test_select_on_a_busy_charger_reassigns_now() -> None:
    sm = _sessions()
    sm.update({"cp5": True}, T0)
    changes = sm.select("scooter", "cp5", T0)
    assert SessionChange("cp5", "scooter", True) in changes
    assert sm.status("scooter")["state"] == "plugged"
    assert sm.status("bandit")["by"] == "scooter"


def test_last_selection_wins_and_selecting_the_default_takes_it_back() -> None:
    sm = _sessions()
    sm.select("scooter", "cp5", T0)
    assert sm.status("scooter") == {"charger_id": "cp5", "state": "selected"}
    sm.select(None, "cp5", T0)                       # then a guest: the guest wins
    assert sm.status("scooter") == {"charger_id": "cp4", "state": "default"}
    sm.select("bandit", "cp5", T0)                  # bandit takes its charger back
    assert sm.next_vehicle == {}
    assert sm.status("bandit") == {"charger_id": "cp5", "state": "default"}


def test_select_none_drops_the_selection() -> None:
    sm = _sessions()
    sm.select("scooter", "cp6", T0)
    sm.select("scooter", None, T0)
    assert sm.status("scooter") == {"charger_id": "cp4", "state": "default"}
    sm.select(None, "cp6", T0)
    sm.select(None, "cp4", T0)
    sm.select(None, None, T0)                        # every guest selection
    assert sm.guest_status() == []
    with pytest.raises(ValueError, match="unknown vehicle"):
        sm.select("tesla", "cp5", T0)


def test_a_vehicle_without_default_charger() -> None:
    sm = SessionManager(default_vehicles={"cp6": None}, vehicles=frozenset({"leaf"}))
    assert sm.status("leaf") == {"charger_id": None, "state": "none"}
    sm.select("leaf", "cp6", T0)
    assert sm.status("leaf") == {"charger_id": "cp6", "state": "selected"}


# ── Guest car: PV or charge now ──────────────────────────────────────────────


def test_guest_mode_lives_until_the_guest_leaves() -> None:
    sm = _sessions()
    with pytest.raises(ValueError, match="put the guest on it first"):
        sm.set_guest_mode("cp5", GUEST_INSTANT)
    sm.select(None, "cp5", T0)                           # expected next
    sm.set_guest_mode("cp5", GUEST_INSTANT)
    sm.update({"cp5": True}, T0)                         # arrives: keeps the mode
    assert sm.guest_modes == {"cp5": GUEST_INSTANT}
    sm.update({"cp5": False}, T0)                        # leaves: mode dropped
    assert sm.guest_modes == {}
    sm.select(None, "cp4", T0)
    with pytest.raises(ValueError, match="guest mode must be"):
        sm.set_guest_mode("cp4", "off")


def test_guest_mode_dropped_when_a_car_takes_the_charger_back() -> None:
    sm = _sessions()
    sm.update({"cp5": True}, T0)
    sm.select(None, "cp5", T0)                           # guest at bandit's charger
    sm.set_guest_mode("cp5", GUEST_INSTANT)
    sm.select("bandit", "cp5", T0)                      # bandit takes it back
    assert sm.guest_modes == {}


async def test_guest_mode_drives_the_contributor_and_is_stored() -> None:
    app, _charger = _live_app()
    app._apply_sessions = lambda: app._fleet.apply(app._disabled_chargepoints, app._ev_force_charge)
    app._storage.save_guest_modes = _store(app._storage, "guest_modes")
    states = _states(True, 0.0, 40.0, T0)
    await app._on_session_changes(app._fleet.compose(states, T0), replan=False, now=T0)
    await app.select_charger(None, "wallbox")            # the car there is a guest
    contrib = app._fleet.contributors["wallbox"]
    assert contrib._guest_mode == "pv_only"              # the charger's configured default
    await app.set_guest_mode(None, GUEST_INSTANT)        # wherever the guest is
    assert contrib._guest_mode == GUEST_INSTANT
    assert app._storage.guest_modes == {"wallbox": GUEST_INSTANT}
    await app.select_charger("ev1", "wallbox")
    assert app._storage.guest_modes == {}


def _store(storage, name):
    async def save(value) -> None:
        setattr(storage, name, dict(value))
    return save

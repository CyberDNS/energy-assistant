"""Tests for EV MQTT Discovery: availability semantics and weekly-plan entities."""

from datetime import datetime, timedelta, timezone

from energy_assistant.assets.ev import EvChargingAsset, EvWeeklyTarget
from energy_assistant.core.models import (
    EV_DISPLAY_MODES,
    ControlIntent,
    DeviceState,
    EnergyPlan,
    ev_current_mode,
)
from energy_assistant.plugins.mqtt_publisher import (
    _STATUS_TOPIC,
    CHARGING_MODES,
    EvMqttPublisher,
    charging_mode_from_setpoint,
    _build_state_messages,
    _discovery_configs,
    _parse_weekly_command,
    _reading_messages,
    _weekly_discovery_configs,
    _weekly_state_messages,
)


def _asset() -> EvChargingAsset:
    return EvChargingAsset(
        asset_id="scooter", device_id="cp1", label="Scooter",
        capacity_kwh=50.0, max_charge_kw=11.0,
    )


def _msgs(state: DeviceState | None) -> dict[str, str]:
    return dict(_reading_messages(_asset(), state))


def test_unplugged_car_with_soc_stays_available() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=62.0, available=False, extra={"plugged": False},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/scooter/available"] == "online"
    assert msgs["energy_assistant/ev/scooter/soc"] == "62"
    assert msgs["energy_assistant/ev/scooter/plugged"] == "OFF"
    assert "energy_assistant/ev/scooter/charging_mode" not in msgs


def test_plan_messages_exclude_live_readings() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=62.0, available=True,
        extra={"plugged": True, "range_km": 300.0},
    )
    topics = {t for t, _, _ in _build_state_messages(_asset(), None, state, None, False)}
    for key in ("available", "soc", "range", "range_available", "plugged"):
        assert f"energy_assistant/ev/scooter/{key}" not in topics
    assert "energy_assistant/ev/scooter/override_active" in topics


def test_plugged_car_reports_plugged_on() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=40.0, available=True, extra={"plugged": True},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/scooter/available"] == "online"
    assert msgs["energy_assistant/ev/scooter/plugged"] == "ON"


def test_range_published_when_reported() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=70.0, available=True,
        extra={"plugged": True, "range_km": 341.6},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/scooter/range_available"] == "online"
    assert msgs["energy_assistant/ev/scooter/range"] == "342"


def test_missing_range_marks_sensor_unavailable() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=70.0, available=True,
        extra={"plugged": True, "range_km": None},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/scooter/range_available"] == "offline"
    assert "energy_assistant/ev/scooter/range" not in msgs
    assert _msgs(None)["energy_assistant/ev/scooter/range_available"] == "offline"


def test_range_discovery_is_distance_sensor() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _discovery_configs(_asset(), {})}
    cfg = configs["ea_ev_scooter_range"]
    assert cfg["state_topic"] == "energy_assistant/ev/scooter/range"
    assert cfg["device_class"] == "distance"
    assert cfg["unit_of_measurement"] == "km"
    assert [a["topic"] for a in cfg["availability"]] == [
        _STATUS_TOPIC, "energy_assistant/ev/scooter/range_available",
    ]
    assert cfg["availability_mode"] == "all"


def test_unreadable_device_goes_offline() -> None:
    msgs = _msgs(DeviceState(device_id="cp1", available=False))
    assert msgs["energy_assistant/ev/scooter/available"] == "offline"
    assert "energy_assistant/ev/scooter/plugged" not in msgs
    assert _msgs(None)["energy_assistant/ev/scooter/available"] == "offline"


def test_discovery_availability_split() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _discovery_configs(_asset(), {})}
    # Live readings require both bridge status and data availability
    for uid in ("ea_ev_scooter_soc", "ea_ev_scooter_plugged"):
        topics = [a["topic"] for a in configs[uid]["availability"]]
        assert topics == [_STATUS_TOPIC, "energy_assistant/ev/scooter/available"]
        assert configs[uid]["availability_mode"] == "all"
    # Setters / goal entities only depend on Energy Assistant being up
    for uid in ("ea_ev_scooter_target_soc_set", "ea_ev_scooter_override",
                "ea_ev_scooter_charging_mode", "ea_ev_scooter_charging_state",
                "ea_ev_scooter_deadline"):
        assert configs[uid]["availability_topic"] == _STATUS_TOPIC


# ── Weekly plan entities ──────────────────────────────────────────────────────

def test_weekly_discovery_has_three_entities_per_weekday() -> None:
    configs = dict(_weekly_discovery_configs(_asset(), {}))
    assert len(configs) == 21
    soc = configs["homeassistant/number/ea_ev_scooter_weekly_mon_soc/config"]
    assert soc["state_topic"] == "energy_assistant/ev/scooter/weekly/mon/soc/state"
    assert soc["command_topic"] == "energy_assistant/ev/scooter/weekly/mon/soc/set"
    assert soc["availability_topic"] == _STATUS_TOPIC
    assert "homeassistant/time/ea_ev_scooter_weekly_sun_time/config" in configs
    assert "homeassistant/switch/ea_ev_scooter_weekly_sun_enabled/config" in configs
    # …and they are part of the full discovery payload
    all_uids = {cfg["unique_id"] for _, cfg in _discovery_configs(_asset(), {})}
    assert {cfg["unique_id"] for cfg in configs.values()} <= all_uids


def test_weekly_state_uses_stored_rows_and_defaults() -> None:
    weekly = {1: EvWeeklyTarget(weekday=1, enabled=True, target_soc_pct=80.0, target_by="07:30")}
    msgs = {t: p for t, p, _ in _weekly_state_messages(_asset(), weekly)}
    base = "energy_assistant/ev/scooter/weekly"
    assert msgs[f"{base}/mon/soc/state"] == "80"
    assert msgs[f"{base}/mon/time/state"] == "07:30:00"
    assert msgs[f"{base}/mon/enabled/state"] == "ON"
    # Unset weekday: disabled, charge limit by 06:00 (same default as the REST API)
    assert msgs[f"{base}/tue/soc/state"] == "100"
    assert msgs[f"{base}/tue/time/state"] == "06:00:00"
    assert msgs[f"{base}/tue/enabled/state"] == "OFF"
    assert len(msgs) == 21


def test_parse_weekly_command() -> None:
    assert _parse_weekly_command("wed", "soc", "85") == (3, {"target_soc_pct": 85.0})
    assert _parse_weekly_command("sun", "time", "06:45:00") == (7, {"target_by": "06:45"})
    assert _parse_weekly_command("mon", "enabled", "ON") == (1, {"enabled": True})
    assert _parse_weekly_command("mon", "enabled", "OFF") == (1, {"enabled": False})
    for bad in (("xyz", "soc", "80"), ("mon", "soc", "150"), ("mon", "soc", "abc"),
                ("mon", "time", "25:00"), ("mon", "nope", "1")):
        assert _parse_weekly_command(*bad) is None


async def test_weekly_command_dispatches_to_callback() -> None:
    calls: list[tuple] = []

    async def on_set_weekly(asset_id, weekday, changes):
        calls.append((asset_id, weekday, changes))

    async def noop(*_a):
        pass

    pub = EvMqttPublisher(None, [_asset()], noop, noop, noop, on_set_weekly=on_set_weekly)
    await pub._dispatch("energy_assistant/ev/scooter/weekly/fri/time/set", "05:15:00")
    await pub._dispatch("energy_assistant/ev/scooter/weekly/fri/soc/set", "999")   # rejected
    await pub._dispatch("energy_assistant/ev/unknown/weekly/fri/soc/set", "80")      # unknown asset
    assert calls == [("scooter", 5, {"target_by": "05:15"})]


# ── Live charging state / mode ────────────────────────────────────────────────

def test_charging_sensors_are_enums_with_documented_options() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _discovery_configs(_asset(), {})}
    state = configs["ea_ev_scooter_charging_state"]
    assert state["device_class"] == "enum"
    assert state["options"] == list(EV_DISPLAY_MODES)
    assert state["state_topic"] == "energy_assistant/ev/scooter/charging_state"
    mode = configs["ea_ev_scooter_charging_mode"]
    assert mode["device_class"] == "enum"
    assert mode["options"] == list(CHARGING_MODES)


def test_charging_mode_from_setpoint() -> None:
    assert charging_mode_from_setpoint(11_000.0, disabled=False) == "instant_charging"
    assert charging_mode_from_setpoint(1.0, disabled=False) == "pv_charging"
    assert charging_mode_from_setpoint(0.0, disabled=False) == "stop"
    assert charging_mode_from_setpoint(None, disabled=False) == "disconnected"
    assert charging_mode_from_setpoint(None, disabled=True) == "disabled"


def _intent(ts, kw, grid):
    return ControlIntent(device_id="cp1", timestep=ts, power_kw=kw, grid_allowed=grid)


def test_ev_current_mode_follows_active_slot() -> None:
    t0 = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=15)
    other = ControlIntent(device_id="bat", timestep=t0 + step, power_kw=0.0)
    plan = EnergyPlan(intents=[
        _intent(t0, 10.0, True),            # grid charge
        other,                               # EV idle in this slot
        _intent(t0 + 2 * step, 3.0, False),  # PV charge
    ])
    assert ev_current_mode(plan, "cp1", None, t0 + timedelta(minutes=5)) == "charge_from_grid"
    assert ev_current_mode(plan, "cp1", None, t0 + step) == "idle"
    assert ev_current_mode(plan, "cp1", None, t0 + 2 * step) == "charge_from_pv"
    assert ev_current_mode(plan, "cp1", t0 + 2 * step, t0 + 2 * step) == "charge_phase2"
    assert ev_current_mode(plan, "cp1", None, t0 - step) == "charge_from_grid"  # plan in future
    assert ev_current_mode(None, "cp1", None, t0) == "idle"
    # Excluded from planning → status comes from the chargepoint flags.
    assert ev_current_mode(plan, "cp1", None, t0, force_charge=True) == "force_charging"
    assert ev_current_mode(None, "cp1", None, t0, disabled=True) == "disabled"
    assert ev_current_mode(plan, "cp1", None, t0, force_charge=True, disabled=True) == "disabled"


async def test_live_states_are_deduped_and_replayed_on_reconnect() -> None:
    async def noop(*_a):
        pass

    pub = EvMqttPublisher(None, [_asset()], noop, noop, noop)
    await pub.publish_live_states({"scooter": ("charge_from_grid", "instant_charging")})
    await pub.publish_live_states({"scooter": ("charge_from_grid", "instant_charging")})
    await pub.publish_live_states({"scooter": ("idle", None)})
    queued = []
    while not pub._queue.empty():
        queued.append(pub._queue.get_nowait()[:2])
    assert queued == [
        ("energy_assistant/ev/scooter/charging_state", "charge_from_grid"),
        ("energy_assistant/ev/scooter/charging_mode", "instant_charging"),
        ("energy_assistant/ev/scooter/charging_state", "idle"),
    ]
    # Cache holds the latest value per topic for re-publishing on reconnect.
    assert pub._live_states == {
        "energy_assistant/ev/scooter/charging_state": "idle",
        "energy_assistant/ev/scooter/charging_mode": "instant_charging",
    }


async def test_readings_are_published_only_on_change() -> None:
    async def noop(*_a):
        pass

    pub = EvMqttPublisher(None, [_asset()], noop, noop, noop)

    def state(soc: float, range_km: float | None) -> dict[str, DeviceState]:
        return {"cp1": DeviceState(
            device_id="cp1", soc_pct=soc, available=True,
            extra={"plugged": True, "range_km": range_km},
        )}

    await pub.publish_readings(state(70.0, 342.0))
    await pub.publish_readings(state(70.2, 342.4))  # same rounded values
    await pub.publish_readings(state(71.0, 340.0))
    await pub.publish_readings(state(71.0, None))   # range lost
    queued = []
    while not pub._queue.empty():
        queued.append(pub._queue.get_nowait()[:2])
    base = "energy_assistant/ev/scooter"
    assert queued == [
        (f"{base}/available", "online"),
        (f"{base}/soc", "70"),
        (f"{base}/range_available", "online"),
        (f"{base}/range", "342"),
        (f"{base}/plugged", "ON"),
        (f"{base}/soc", "71"),
        (f"{base}/range", "340"),
        (f"{base}/range_available", "offline"),
    ]

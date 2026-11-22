"""Tests for EV MQTT Discovery: availability semantics and weekly-plan entities."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from energy_assistant.assets.ev import EvChargingAsset, EvWeeklyTarget
from energy_assistant.core.device_model import DeviceCategory, DeviceSpec
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
    DiscoveryDevice,
    EvMqttPublisher,
    _build_state_messages,
    _discovery_configs,
    _parse_weekly_command,
    _reading_messages,
    _stale_vehicle_topics,
    _weekly_discovery_configs,
    _weekly_state_messages,
    charging_mode_from_setpoint,
    select_discovery_devices,
)


def _asset() -> EvChargingAsset:
    return EvChargingAsset(
        asset_id="scooter", device_id="cp1", label="Scooter",
        capacity_kwh=50.0, max_charge_kw=11.0,
    )


def _msgs(state: DeviceState | None) -> dict[str, str]:
    """Readings of the vehicle at its charger: *state* stands for both (the
    vehicle's SoC and range, the charger's plug state)."""
    return dict(_reading_messages(_asset(), state, state))


def test_away_vehicle_reports_unplugged_and_away() -> None:
    asset = replace(_asset(), device_id="")
    vehicle = DeviceState(device_id="scooter", soc_pct=55.0, extra={"range_km": 200.0})
    msgs = dict(_reading_messages(asset, vehicle, None))
    assert msgs["energy_assistant/vehicle/scooter/soc"] == "55"
    assert msgs["energy_assistant/vehicle/scooter/plugged"] == "OFF"
    assert msgs["energy_assistant/vehicle/scooter/location"] == "away"
    assert dict(_reading_messages(_asset(), vehicle, None))[
        "energy_assistant/vehicle/scooter/location"] == "cp1"


def test_unplugged_car_with_soc_stays_available() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=62.0, available=False, extra={"plugged": False},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/vehicle/scooter/available"] == "online"
    assert msgs["energy_assistant/vehicle/scooter/soc"] == "62"
    assert msgs["energy_assistant/vehicle/scooter/plugged"] == "OFF"
    assert "energy_assistant/vehicle/scooter/charging_mode" not in msgs


def test_plan_messages_exclude_live_readings() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=62.0, available=True,
        extra={"plugged": True, "range_km": 300.0},
    )
    topics = {t for t, _, _ in _build_state_messages(_asset(), None, state, None, False)}
    for key in ("available", "soc", "range", "range_available", "plugged"):
        assert f"energy_assistant/vehicle/scooter/{key}" not in topics
    assert "energy_assistant/vehicle/scooter/override_active" in topics


def test_plugged_car_reports_plugged_on() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=40.0, available=True, extra={"plugged": True},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/vehicle/scooter/available"] == "online"
    assert msgs["energy_assistant/vehicle/scooter/plugged"] == "ON"


def test_range_published_when_reported() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=70.0, available=True,
        extra={"plugged": True, "range_km": 341.6},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/vehicle/scooter/range_available"] == "online"
    assert msgs["energy_assistant/vehicle/scooter/range"] == "342"


def test_missing_range_marks_sensor_unavailable() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=70.0, available=True,
        extra={"plugged": True, "range_km": None},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/vehicle/scooter/range_available"] == "offline"
    assert "energy_assistant/vehicle/scooter/range" not in msgs
    assert _msgs(None)["energy_assistant/vehicle/scooter/range_available"] == "offline"


def test_range_discovery_is_distance_sensor() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _discovery_configs(_asset(), {})}
    cfg = configs["ea_ev_scooter_range"]
    assert cfg["state_topic"] == "energy_assistant/vehicle/scooter/range"
    assert cfg["device_class"] == "distance"
    assert cfg["unit_of_measurement"] == "km"
    assert [a["topic"] for a in cfg["availability"]] == [
        _STATUS_TOPIC, "energy_assistant/vehicle/scooter/range_available",
    ]
    assert cfg["availability_mode"] == "all"


def test_unreadable_device_goes_offline() -> None:
    msgs = _msgs(DeviceState(device_id="cp1", available=False))
    assert msgs["energy_assistant/vehicle/scooter/available"] == "offline"
    assert "energy_assistant/vehicle/scooter/plugged" not in msgs
    assert _msgs(None)["energy_assistant/vehicle/scooter/available"] == "offline"


def test_discovery_availability_split() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _discovery_configs(_asset(), {})}
    # Live readings require both bridge status and data availability
    for uid in ("ea_ev_scooter_soc", "ea_ev_scooter_plugged"):
        topics = [a["topic"] for a in configs[uid]["availability"]]
        assert topics == [_STATUS_TOPIC, "energy_assistant/vehicle/scooter/available"]
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
    assert soc["state_topic"] == "energy_assistant/vehicle/scooter/weekly/mon/soc/state"
    assert soc["command_topic"] == "energy_assistant/vehicle/scooter/weekly/mon/soc/set"
    assert soc["availability_topic"] == _STATUS_TOPIC
    assert "homeassistant/time/ea_ev_scooter_weekly_sun_time/config" in configs
    assert "homeassistant/switch/ea_ev_scooter_weekly_sun_enabled/config" in configs
    # …and they are part of the full discovery payload
    all_uids = {cfg["unique_id"] for _, cfg in _discovery_configs(_asset(), {})}
    assert {cfg["unique_id"] for cfg in configs.values()} <= all_uids


def test_weekly_state_uses_stored_rows_and_defaults() -> None:
    weekly = {1: EvWeeklyTarget(weekday=1, enabled=True, target_soc_pct=80.0, target_by="07:30")}
    msgs = {t: p for t, p, _ in _weekly_state_messages(_asset(), weekly)}
    base = "energy_assistant/vehicle/scooter/weekly"
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
    await pub._dispatch("energy_assistant/vehicle/scooter/weekly/fri/time/set", "05:15:00")
    await pub._dispatch("energy_assistant/vehicle/scooter/weekly/fri/soc/set", "999")   # rejected
    await pub._dispatch("energy_assistant/vehicle/unknown/weekly/fri/soc/set", "80")      # unknown asset
    assert calls == [("scooter", 5, {"target_by": "05:15"})]


# ── Live charging state / mode ────────────────────────────────────────────────

def test_charging_sensors_are_enums_with_documented_options() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _discovery_configs(_asset(), {})}
    state = configs["ea_ev_scooter_charging_state"]
    assert state["device_class"] == "enum"
    assert state["options"] == list(EV_DISPLAY_MODES)
    assert state["state_topic"] == "energy_assistant/vehicle/scooter/charging_state"
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
        ("energy_assistant/vehicle/scooter/charging_state", "charge_from_grid"),
        ("energy_assistant/vehicle/scooter/charging_mode", "instant_charging"),
        ("energy_assistant/vehicle/scooter/charging_state", "idle"),
    ]
    # Cache holds the latest value per topic for re-publishing on reconnect.
    assert pub._live_states == {
        "energy_assistant/vehicle/scooter/charging_state": "idle",
        "energy_assistant/vehicle/scooter/charging_mode": "instant_charging",
    }


async def test_readings_are_published_only_on_change() -> None:
    async def noop(*_a):
        pass

    pub = EvMqttPublisher(None, [_asset()], noop, noop, noop)

    def state(soc: float, range_km: float | None) -> dict[str, DeviceState]:
        return {
            "scooter": DeviceState(device_id="scooter", soc_pct=soc, extra={"range_km": range_km}),
            "cp1": DeviceState(device_id="cp1", available=True, extra={"plugged": True}),
        }

    await pub.publish_readings(state(70.0, 342.0))
    await pub.publish_readings(state(70.2, 342.4))  # same rounded values
    await pub.publish_readings(state(71.0, 340.0))
    await pub.publish_readings(state(71.0, None))   # range lost
    queued = []
    while not pub._queue.empty():
        queued.append(pub._queue.get_nowait()[:2])
    base = "energy_assistant/vehicle/scooter"
    assert queued == [
        (f"{base}/available", "online"),
        (f"{base}/soc", "70"),
        (f"{base}/range_available", "online"),
        (f"{base}/range", "342"),
        (f"{base}/plugged", "ON"),
        (f"{base}/location", "cp1"),
        (f"{base}/charging_power", "0.00"),
        (f"{base}/charging_now", "OFF"),
        (f"{base}/soc", "71"),
        (f"{base}/range", "340"),
        (f"{base}/range_available", "offline"),
    ]


# ── Device sub-devices ────────────────────────────────────────────────────────

def test_select_discovery_devices_skips_ha_and_virtual_devices() -> None:
    def spec(device_id: str, type_name: str, category=DeviceCategory.METER, **kw) -> DeviceSpec:
        return DeviceSpec(id=device_id, category=category, type=type_name, **kw)

    cfg = {
        "main_grid_meter": spec("main_grid_meter", "generic_homeassistant"),
        "cooler": spec("cooler", "homeassistant_thermal", DeviceCategory.THERMAL_LOAD),
        "cooler_plug": spec("cooler_plug", "homeassistant_switch", DeviceCategory.SWITCH),
        "baseline": spec("baseline", "generic_consumer", DeviceCategory.CONSUMER),
        "sma_battery": spec("sma_battery", "modbus", DeviceCategory.BATTERY, label="SMA Speicher"),
        "household_meter": spec("household_meter", "tibber_local"),
        "hidden": spec("hidden", "zendure", DeviceCategory.BATTERY, ha_discovery=False),
        "broken": spec("broken", "modbus", DeviceCategory.BATTERY),  # failed to build
    }
    built = {k for k in cfg if k != "broken"}
    picked = select_discovery_devices(cfg, built)
    assert [(d.device_id, d.name, d.model) for d in picked] == [
        ("sma_battery", "SMA Speicher", "modbus"),
        ("household_meter", "Household Meter", "tibber_local"),
    ]
    assert picked[0].category is DeviceCategory.BATTERY


def _noop_pub(devices: list[DiscoveryDevice]) -> EvMqttPublisher:
    async def noop(*_a):
        pass

    return EvMqttPublisher(None, [], noop, noop, noop, devices=devices)


def _drain(pub: EvMqttPublisher) -> list[tuple[str, str]]:
    out = []
    while not pub._queue.empty():
        out.append(pub._queue.get_nowait()[:2])
    return out


async def test_device_sensors_are_discovered_lazily_as_subdevices() -> None:
    bat = DiscoveryDevice("sma_battery", "SMA Battery", DeviceCategory.BATTERY, "modbus")
    pub = _noop_pub([bat])
    await pub.publish_readings({"sma_battery": DeviceState(
        device_id="sma_battery", power_w=-1234.4, soc_pct=55.25,
        extra={"max_cha_w": 3000, "unknown_key": 1.0, "chargemode": "pv"},
    )})
    queued = _drain(pub)
    configs = {t: json.loads(p) for t, p in queued if t.startswith("homeassistant/")}
    assert set(configs) == {
        "homeassistant/sensor/ea_dev_sma_battery_power_w/config",
        "homeassistant/sensor/ea_dev_sma_battery_soc_pct/config",
        "homeassistant/sensor/ea_dev_sma_battery_max_cha_w/config",
    }
    power = configs["homeassistant/sensor/ea_dev_sma_battery_power_w/config"]
    assert power["device"]["via_device"] == "energy_assistant_kpis"
    assert power["device"]["identifiers"] == ["energy_assistant_device_sma_battery"]
    assert power["device_class"] == "power" and power["unit_of_measurement"] == "W"
    assert [a["topic"] for a in power["availability"]] == [
        _STATUS_TOPIC, "energy_assistant/device/sma_battery/available",
    ]
    states = {t: p for t, p in queued if not t.startswith("homeassistant/")}
    assert states == {
        "energy_assistant/device/sma_battery/available": "online",
        "energy_assistant/device/sma_battery/power_w": "-1234",
        "energy_assistant/device/sma_battery/soc_pct": "55.2",
        "energy_assistant/device/sma_battery/max_cha_w": "3000",
    }

    # Same values again: nothing new.  A new field: only its config + value.
    await pub.publish_readings({"sma_battery": DeviceState(
        device_id="sma_battery", power_w=-1234.0, soc_pct=55.2, energy_kwh=12.5,
        extra={"max_cha_w": 3000},
    )})
    assert _drain(pub) == [
        ("homeassistant/sensor/ea_dev_sma_battery_energy_kwh/config",
         pub._device_configs["homeassistant/sensor/ea_dev_sma_battery_energy_kwh/config"]),
        ("energy_assistant/device/sma_battery/energy_kwh", "12.500"),
    ]


async def test_chargepoint_stays_available_without_car_and_skips_soc() -> None:
    cp = DiscoveryDevice("cp5", "Chargepoint 5", DeviceCategory.CHARGER, "openwb_mqtt")
    pub = _noop_pub([cp])
    await pub.publish_readings({"cp5": DeviceState(
        device_id="cp5", power_w=0.0, soc_pct=80.0, available=False,
        extra={"plugged": False},
    )})
    states = {t: p for t, p in _drain(pub) if not t.startswith("homeassistant/")}
    assert states == {
        "energy_assistant/device/cp5/available": "online",
        "energy_assistant/device/cp5/power_w": "0",
    }
    # Unreachable (no readings at all) → offline; missing state → offline.
    await pub.publish_readings({"cp5": DeviceState(device_id="cp5", available=False)})
    assert _drain(pub) == [("energy_assistant/device/cp5/available", "offline")]


# ── Chargers: vehicle / next vehicle selects; per-car charging entities ─────

def test_vehicle_charger_select_and_guest_select() -> None:
    from energy_assistant.plugins.mqtt_publisher import (
        _charger_of_option,
        _charger_select_state,
        _guest_select_config,
    )

    chargers = [("cp5", "Garage A"), ("cp4", "Garage B")]
    configs = {c["unique_id"]: c for _, c in _discovery_configs(_asset(), {}, None, chargers)}
    sel = configs["ea_ev_scooter_charger"]
    assert sel["options"] == ["Garage A", "Garage B", "No charger"]
    assert sel["command_topic"] == "energy_assistant/vehicle/scooter/charger_select/set"
    assert "ea_ev_scooter_location" in configs
    assert "ea_ev_scooter_charger" not in {
        c["unique_id"] for _, c in _discovery_configs(_asset(), {})}   # no chargers: no select
    assert _guest_select_config(chargers)[1]["options"] == ["No charger", "Garage A", "Garage B"]

    assert _charger_select_state({"charger_id": "cp4", "state": "plugged"}, chargers) == "Garage B"
    assert _charger_select_state({"charger_id": "cp4", "state": "default"}, chargers) == "Garage B"
    assert _charger_select_state({"charger_id": "cp4", "state": "taken", "by": "guest"}, chargers) == "No charger"
    assert _charger_select_state(None, chargers) == "No charger"
    assert _charger_of_option("Garage A", chargers) == "cp5"
    assert _charger_of_option("No charger", chargers) is None
    assert _charger_of_option("Carport", chargers) is False


def test_vehicle_charging_power_follows_the_charger() -> None:
    vehicle = DeviceState(device_id="scooter", soc_pct=55.0)
    charger = DeviceState(device_id="cp1", power_w=7400.0, extra={"plugged": True})
    msgs = dict(_reading_messages(_asset(), vehicle, charger, "Garage B"))
    base = "energy_assistant/vehicle/scooter"
    assert msgs[f"{base}/location"] == "Garage B"
    assert msgs[f"{base}/charging_power"] == "7.40"
    assert msgs[f"{base}/charging_now"] == "ON"
    away = dict(_reading_messages(replace(_asset(), device_id=""), vehicle, None))
    assert away[f"{base}/charging_power"] == "0.00" and away[f"{base}/charging_now"] == "OFF"


async def test_charger_select_commands_call_back() -> None:
    calls = []

    async def noop(*_a):
        pass

    async def on_select(vid, cid):
        calls.append((vid, cid))

    pub = EvMqttPublisher(None, [_asset()], noop, noop, noop,
                          chargers=[("cp5", "Garage A")], on_select_charger=on_select)
    await pub._dispatch("energy_assistant/vehicle/scooter/charger_select/set", "Garage A")
    await pub._dispatch("energy_assistant/guest/charger_select/set", "Garage A")
    await pub._dispatch("energy_assistant/guest/charger_select/set", "No charger")
    await pub._dispatch("energy_assistant/vehicle/scooter/charger_select/set", "Carport")
    assert calls == [("scooter", "cp5"), (None, "cp5"), (None, None)]


async def test_guest_mode_select() -> None:
    from energy_assistant.plugins.mqtt_publisher import _guest_mode_select_config

    assert _guest_mode_select_config()[1]["options"] == ["PV", "Charge now"]
    modes = []

    async def noop(*_a):
        pass

    async def on_mode(mode):
        modes.append(mode)

    pub = EvMqttPublisher(None, [], noop, noop, noop, chargers=[("cp5", "Garage A")],
                          on_set_guest_mode=on_mode)
    await pub._dispatch("energy_assistant/guest/mode/set", "Charge now")
    await pub._dispatch("energy_assistant/guest/mode/set", "Turbo")
    assert modes == ["instant"]
    await pub.publish_readings({"cp5": DeviceState(
        device_id="cp5", extra={"guest_here": True, "guest_mode": "instant"})})
    queued = dict(pub._queue.get_nowait()[:2] for _ in range(pub._queue.qsize()))
    assert queued["energy_assistant/guest/charger_select"] == "Garage A"
    assert queued["energy_assistant/guest/mode"] == "Charge now"


class _Msg:
    def __init__(self, topic: str, payload: bytes = b"{}", retain: bool = True) -> None:
        self.topic, self.payload, self.retain = topic, payload, retain


class _RetainedClient:
    """An MQTT client whose broker holds the given retained messages."""

    def __init__(self, retained: list[_Msg]) -> None:
        self._retained = retained
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    async def unsubscribe(self, topic: str) -> None:
        self.unsubscribed.append(topic)

    async def publish(self, topic, payload, qos=0, retain=False) -> None:
        self.published.append((topic, payload, retain))

    @property
    def messages(self):
        async def gen():
            for msg in self._retained:
                yield msg
            await asyncio.sleep(3600)   # the broker has nothing more
        return gen()


async def test_stale_vehicle_topics_are_the_retained_ones_no_longer_published() -> None:
    client = _RetainedClient([
        _Msg("homeassistant/sensor/ea_ev_ev_bandit_soc/config"),       # former id
        _Msg("homeassistant/switch/ea_ev_ev_bandit_weekly_mon_enabled/config"),
        _Msg("homeassistant/sensor/ea_ev_bandit_soc/config"),          # still published
        _Msg("homeassistant/sensor/ea_ev_gone_soc/config", b""),        # already cleared
        _Msg("homeassistant/sensor/ea_dev_meter_power_w/config"),       # not a vehicle
        _Msg("homeassistant/sensor/zigbee_thing/config"),               # someone else's
        _Msg("homeassistant/sensor/ea_ev_live_soc/config", retain=False),
    ])

    stale = await _stale_vehicle_topics(
        client, {"homeassistant/sensor/ea_ev_bandit_soc/config"}, quiet_s=0.05
    )

    assert stale == {
        "homeassistant/sensor/ea_ev_ev_bandit_soc/config",
        "homeassistant/switch/ea_ev_ev_bandit_weekly_mon_enabled/config",
    }
    assert client.subscribed == client.unsubscribed == ["homeassistant/+/+/config"]


async def test_discovery_removes_stale_vehicle_entities_before_publishing() -> None:
    pub = _noop_pub([])
    pub._assets = [_asset()]
    pub._remove_stale_vehicles = True
    old = "homeassistant/sensor/ea_ev_old_car_soc/config"
    client = _RetainedClient([_Msg(old)])

    await pub._publish_discovery(client)

    topics = [t for t, _p, _r in client.published]
    assert client.published[0] == (old, "", True)
    own = f"homeassistant/sensor/ea_ev_{_asset().asset_id}_soc/config"
    assert topics.index(old) < topics.index(own)

    # A dry run leaves other instances' entities alone.
    pub._remove_stale_vehicles = False
    client = _RetainedClient([_Msg(old)])
    await pub._publish_discovery(client)
    assert old not in [t for t, _p, _r in client.published]

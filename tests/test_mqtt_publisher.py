"""Tests for EV MQTT Discovery: availability semantics and weekly-plan entities."""

from energy_assistant.assets.ev import EvChargingAsset, EvWeeklyTarget
from energy_assistant.core.models import DeviceState
from energy_assistant.plugins.mqtt_publisher import (
    _STATUS_TOPIC,
    EvMqttPublisher,
    _build_state_messages,
    _discovery_configs,
    _parse_weekly_command,
    _weekly_discovery_configs,
    _weekly_state_messages,
)


def _asset() -> EvChargingAsset:
    return EvChargingAsset(
        asset_id="schlumpf", device_id="cp1", label="Schlumpf",
        capacity_kwh=50.0, max_charge_kw=11.0,
    )


def _msgs(state: DeviceState | None) -> dict[str, str]:
    return {t: p for t, p, _ in _build_state_messages(_asset(), None, state, None, False)}


def test_unplugged_car_with_soc_stays_available() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=62.0, available=False, extra={"plugged": False},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/schlumpf/available"] == "online"
    assert msgs["energy_assistant/ev/schlumpf/soc"] == "62"
    assert msgs["energy_assistant/ev/schlumpf/plugged"] == "OFF"
    assert msgs["energy_assistant/ev/schlumpf/charging_mode"] == "disconnected"


def test_plugged_car_reports_plugged_on() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=40.0, available=True, extra={"plugged": True},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/schlumpf/available"] == "online"
    assert msgs["energy_assistant/ev/schlumpf/plugged"] == "ON"


def test_unreadable_device_goes_offline() -> None:
    msgs = _msgs(DeviceState(device_id="cp1", available=False))
    assert msgs["energy_assistant/ev/schlumpf/available"] == "offline"
    assert "energy_assistant/ev/schlumpf/plugged" not in msgs
    assert _msgs(None)["energy_assistant/ev/schlumpf/available"] == "offline"


def test_discovery_availability_split() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _discovery_configs(_asset(), {})}
    # Live readings require both bridge status and data availability
    for uid in ("ea_ev_schlumpf_soc", "ea_ev_schlumpf_plugged"):
        topics = [a["topic"] for a in configs[uid]["availability"]]
        assert topics == [_STATUS_TOPIC, "energy_assistant/ev/schlumpf/available"]
        assert configs[uid]["availability_mode"] == "all"
    # Setters / goal entities only depend on Energy Assistant being up
    for uid in ("ea_ev_schlumpf_target_soc_set", "ea_ev_schlumpf_override",
                "ea_ev_schlumpf_charging_mode", "ea_ev_schlumpf_deadline"):
        assert configs[uid]["availability_topic"] == _STATUS_TOPIC


# ── Weekly plan entities ──────────────────────────────────────────────────────

def test_weekly_discovery_has_three_entities_per_weekday() -> None:
    configs = dict(_weekly_discovery_configs(_asset(), {}))
    assert len(configs) == 21
    soc = configs["homeassistant/number/ea_ev_schlumpf_weekly_mon_soc/config"]
    assert soc["state_topic"] == "energy_assistant/ev/schlumpf/weekly/mon/soc/state"
    assert soc["command_topic"] == "energy_assistant/ev/schlumpf/weekly/mon/soc/set"
    assert soc["availability_topic"] == _STATUS_TOPIC
    assert "homeassistant/time/ea_ev_schlumpf_weekly_sun_time/config" in configs
    assert "homeassistant/switch/ea_ev_schlumpf_weekly_sun_enabled/config" in configs
    # …and they are part of the full discovery payload
    all_uids = {cfg["unique_id"] for _, cfg in _discovery_configs(_asset(), {})}
    assert {cfg["unique_id"] for cfg in configs.values()} <= all_uids


def test_weekly_state_uses_stored_rows_and_defaults() -> None:
    weekly = {1: EvWeeklyTarget(weekday=1, enabled=True, target_soc_pct=80.0, target_by="07:30")}
    msgs = {t: p for t, p, _ in _weekly_state_messages(_asset(), weekly)}
    base = "energy_assistant/ev/schlumpf/weekly"
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
    await pub._dispatch("energy_assistant/ev/schlumpf/weekly/fri/time/set", "05:15:00")
    await pub._dispatch("energy_assistant/ev/schlumpf/weekly/fri/soc/set", "999")   # rejected
    await pub._dispatch("energy_assistant/ev/unknown/weekly/fri/soc/set", "80")      # unknown asset
    assert calls == [("schlumpf", 5, {"target_by": "05:15"})]

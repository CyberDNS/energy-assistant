"""Tests for EV MQTT Discovery availability semantics."""

from energy_assistant.assets.ev import EvChargingAsset
from energy_assistant.core.models import DeviceState
from energy_assistant.plugins.mqtt_publisher import (
    _STATUS_TOPIC,
    _build_state_messages,
    _discovery_configs,
)


def _asset() -> EvChargingAsset:
    return EvChargingAsset(
        asset_id="scooter", device_id="cp1", label="Scooter",
        capacity_kwh=50.0, max_charge_kw=11.0,
    )


def _msgs(state: DeviceState | None) -> dict[str, str]:
    return {t: p for t, p, _ in _build_state_messages(_asset(), None, state, None, False)}


def test_unplugged_car_with_soc_stays_available() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=62.0, available=False, extra={"plugged": False},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/scooter/available"] == "online"
    assert msgs["energy_assistant/ev/scooter/soc"] == "62"
    assert msgs["energy_assistant/ev/scooter/plugged"] == "OFF"
    assert msgs["energy_assistant/ev/scooter/charging_mode"] == "disconnected"


def test_plugged_car_reports_plugged_on() -> None:
    state = DeviceState(
        device_id="cp1", soc_pct=40.0, available=True, extra={"plugged": True},
    )
    msgs = _msgs(state)
    assert msgs["energy_assistant/ev/scooter/available"] == "online"
    assert msgs["energy_assistant/ev/scooter/plugged"] == "ON"


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
                "ea_ev_scooter_charging_mode", "ea_ev_scooter_deadline"):
        assert configs[uid]["availability_topic"] == _STATUS_TOPIC

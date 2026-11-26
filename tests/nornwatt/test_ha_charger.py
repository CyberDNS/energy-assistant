"""homeassistant_charger: readings and the four control variants (#106 phase 5)."""

from __future__ import annotations

import pytest
from helpers.fake_ha_client import FakeHAClient

from nornwatt.core.config import BackendsConfig
from nornwatt.core.device_model import DeviceConfigError, parse_devices
from nornwatt.core.models import Behaviour, DeviceCommand, resolve_behaviour
from nornwatt.core.plugin_registry import BuildContext
from nornwatt.loader.device_loader import validate_plugins
from nornwatt.plugins import registry

B = Behaviour
READINGS = {"power_w": {"entity": "sensor.wb_power"},
            "plugged": {"entity": "sensor.wb_status", "on": ["Charging", "Wait car"]}}


def _client(**states) -> FakeHAClient:
    client = FakeHAClient({"sensor.wb_power": "0", "sensor.wb_status": "Wait car"})
    client.apply_services = True
    for entity, (state, attrs) in states.items():
        client.set_state(entity.replace("__", "."), state, **attrs)
    return client


def _charger(client: FakeHAClient, setpoint: dict | None, **settings):
    caps = dict(READINGS)
    if setpoint is not None:
        caps["power_setpoint"] = setpoint
    return registry.build_device("wb", {
        "type": "homeassistant_charger", "category": "charger", "capabilities": caps,
        **settings,
    }, BuildContext(backends=BackendsConfig(), ha_client=client))


def _cmd(value: float, behaviour: Behaviour | None = None) -> DeviceCommand:
    return DeviceCommand(device_id="wb", command="set_power_w", value=value, behaviour=behaviour)


def _writes(client: FakeHAClient) -> list[tuple]:
    return [(service, data.get("entity_id"),
             data.get("value", data.get("option", data.get("power_w"))))
            for _d, service, data in client.calls]


async def test_readings_and_plug_state() -> None:
    client = _client()
    dev = _charger(client, None)
    st = await dev.get_state()
    assert (st.available, st.extra["plugged"], st.power_w) == (True, True, 0.0)
    client.set_state("sensor.wb_status", "Idle")
    st = await dev.get_state()
    assert (st.available, st.extra["plugged"]) == (False, False)


async def test_current_control_with_enable_switch() -> None:
    client = _client(number__wb_current=("6", {"min": 6, "max": 16}),
                     switch__wb_charging=("off", {}))
    dev = _charger(client, {"control": "current", "current": "number.wb_current",
                            "enable": "switch.wb_charging"})
    await dev.send_command(_cmd(11_000, B.CHARGE))   # 11 kW / 690 W/A → 16 A
    await dev.send_command(_cmd(7_000, B.CHARGE))    # → 10 A
    await dev.send_command(_cmd(0, B.STOP))
    assert _writes(client) == [
        ("set_value", "number.wb_current", 16.0), ("turn_on", "switch.wb_charging", None),
        ("set_value", "number.wb_current", 10.0),
        ("turn_off", "switch.wb_charging", None),
    ]


async def test_current_control_single_phase_without_switch() -> None:
    client = _client(number__wb_current=("0", {}))
    dev = _charger(client, {"control": "current", "current": "number.wb_current"}, phases=1)
    await dev.send_command(_cmd(3_000, B.CHARGE))    # 3 kW / 230 W/A → 13 A
    await dev.send_command(_cmd(1_000, B.CHARGE))    # < 6 A → stop: current 0
    assert _writes(client) == [("set_value", "number.wb_current", 13.0),
                               ("set_value", "number.wb_current", 0.0)]


async def test_following_the_surplus_with_a_stop_delay() -> None:
    client = _client(number__wb_current=("6", {}), switch__wb_charging=("off", {}))
    dev = _charger(client, {"control": "current", "current": "number.wb_current",
                            "enable": "switch.wb_charging", "stop_delay_s": 60})
    now = [0.0]
    dev.control._clock = lambda: now[0]
    await dev.send_command(_cmd(2_000, B.ABSORB_SURPLUS))   # too little to start
    assert client._states["switch.wb_charging"] == "off"
    await dev.send_command(_cmd(5_000, B.ABSORB_SURPLUS))   # 7 A: start
    assert (client._states["switch.wb_charging"], client._states["number.wb_current"]) == (
        "on", "7.0")
    now[0] = 10
    await dev.send_command(_cmd(3_000, B.ABSORB_SURPLUS))   # a cloud: hold 6 A
    assert (client._states["switch.wb_charging"], client._states["number.wb_current"]) == (
        "on", "6.0")
    now[0] = 75
    await dev.send_command(_cmd(3_000, B.ABSORB_SURPLUS))   # still too little after 60 s
    assert client._states["switch.wb_charging"] == "off"


async def test_power_control_in_kw_and_action_control() -> None:
    client = _client(number__wb_power=("0", {"unit_of_measurement": "kW"}))
    dev = _charger(client, {"control": "power", "power": "number.wb_power"})
    await dev.send_command(_cmd(7_360, B.CHARGE))   # power numbers get the power, in kW
    assert client._states["number.wb_power"] == "7.36"
    await dev.send_command(_cmd(0, B.STOP))
    assert client._states["number.wb_power"] == "0.0"

    client = _client()
    dev = _charger(client, {"control": "action", "action": "script.wallbox",
                            "power_key": "watts"})
    await dev.send_command(_cmd(4_500, B.CHARGE))
    await dev.send_command(_cmd(0, B.STOP))
    assert client.calls == [("script", "wallbox", {"watts": 4_500.0}),
                            ("script", "wallbox", {"watts": 0.0})]


def _openwb_style(client: FakeHAClient):
    return _charger(client, {
        "control": "modes", "mode": "select.mode",
        "modes": {
            "charge": {"option": "Instant Charging", "current": "number.instant_current"},
            "absorb_surplus": {"option": "PV Charging"},
            "stop": {"option": "Stop"},
        },
        "soc_limit": "number.soc_limit", "release": "absorb_surplus",
    })


async def test_mode_control_with_current_and_soc_limit() -> None:
    client = _client(select__mode=("Stop", {"options": ["Instant Charging", "PV Charging", "Stop"]}),
                     number__instant_current=("16", {}), number__soc_limit=("100", {}))
    dev = _openwb_style(client)
    assert dev.supported_behaviours == {B.CHARGE, B.ABSORB_SURPLUS, B.STOP}
    dev.update_target_soc(80.0)
    await dev.send_command(_cmd(7_000, B.CHARGE))
    await dev.send_command(_cmd(1_800, B.ABSORB_SURPLUS))
    await dev.send_command(_cmd(0, B.STOP))
    await dev.release()
    assert _writes(client) == [
        ("set_value", "number.soc_limit", 80.0),
        ("set_value", "number.instant_current", 10.0),
        ("select_option", "select.mode", "Instant Charging"),
        ("select_option", "select.mode", "PV Charging"),
        ("select_option", "select.mode", "Stop"),
        ("select_option", "select.mode", "PV Charging"),
    ]


async def test_mode_control_without_a_pv_mode_charges_the_surplus_by_power() -> None:
    """No absorb_surplus mode: the control loop narrows it to charge with EA's
    surplus estimate, so EA follows the surplus through the charge mode."""
    client = _client(select__mode=("Stop", {}), number__instant_current=("6", {}))
    dev = _charger(client, {"control": "modes", "mode": "select.mode", "modes": {
        "charge": {"option": "Instant", "current": "number.instant_current"},
        "stop": {"option": "Stop"}}})
    behaviour = resolve_behaviour(B.ABSORB_SURPLUS, 4_830.0, dev.supported_behaviours)
    assert behaviour is B.CHARGE
    await dev.send_command(_cmd(4_830.0, behaviour))
    assert (client._states["select.mode"], client._states["number.instant_current"]) == (
        "Instant", "7.0")


async def test_old_openwb_homeassistant_config_is_translated() -> None:
    client = _client(select__lademodus=("Stop", {}))
    dev = registry.build_device("cp", {
        "type": "openwb_homeassistant", "category": "charger",
        "entity_mode": "select.lademodus", "entity_soc": "sensor.cp_soc",
        "mode_pv": "PV", "mode_instant": "Sofort", "mode_stop": "Stop",
    }, BuildContext(backends=BackendsConfig(), ha_client=client))
    st = await dev.get_state()
    assert st.available and st.extra["plugged"]          # no plug sensor: always connected
    await dev.send_command(DeviceCommand(device_id="cp", command="set_power_w", value=0.0,
                                         behaviour=B.ABSORB_SURPLUS))
    assert client._states["select.lademodus"] == "PV"
    assert registry.device_info("openwb_homeassistant").legacy


def test_validation_of_the_power_setpoint() -> None:
    def errors(setpoint: dict) -> list[str]:
        cfg = {"wb": {"category": "charger", "type": "homeassistant_charger",
                      "capabilities": {**READINGS, "power_setpoint": setpoint}}}
        try:
            validate_plugins(parse_devices(cfg))
        except DeviceConfigError as exc:
            return exc.errors
        return []

    assert errors({"control": "current", "current": "number.c", "enable": "switch.e"}) == []
    assert errors({"control": "current"}) == [(
        "device 'wb': capabilities.power_setpoint: key 'current' is required "
        "(Charging current (A))"
    )]
    assert errors({"control": "current", "current": "number.c", "enable": "sensor.e"}) == [(
        "device 'wb': capabilities.power_setpoint: enable must be a switch / input_boolean / "
        "light / fan entity, got 'sensor.e'"
    )]


def test_build_errors() -> None:
    client = _client()
    with pytest.raises(DeviceConfigError, match="must be one of charge, absorb_surplus, stop"):
        _charger(client, {"control": "modes", "mode": "select.m",
                          "modes": {"discharge": {"option": "x"}}})
    with pytest.raises(DeviceConfigError, match="has no mode"):
        _charger(client, {"control": "modes", "mode": "select.m", "release": "stop",
                          "modes": {"charge": {"option": "x"}}})

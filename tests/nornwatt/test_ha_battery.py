"""homeassistant_battery: readings and the five control variants (#106 phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from helpers.fake_ha_client import FakeHAClient

from nornwatt.core.config import BackendsConfig
from nornwatt.core.control import (
    ControlLoop,
    LiveSituation,
    StorageControlContributor,
)
from nornwatt.core.device_model import DeviceConfigError, parse_devices
from nornwatt.core.ledger import BatteryCostLedger
from nornwatt.core.models import (
    Behaviour,
    ControlIntent,
    DeviceCommand,
    DeviceState,
    EnergyPlan,
    StorageConstraints,
)
from nornwatt.core.plugin_registry import BuildContext
from nornwatt.core.registry import DeviceRegistry
from nornwatt.loader.device_loader import validate_plugins
from nornwatt.plugins import registry

B = Behaviour
READINGS = {"power_w": {"entity": "sensor.bat_power"}, "soc_pct": {"entity": "sensor.bat_soc"}}
PARAMS = {"capacity_kwh": 10, "max_charge_kw": 5, "max_discharge_kw": 5}


def _client(**states) -> FakeHAClient:
    client = FakeHAClient({"sensor.bat_power": "-1.2", "sensor.bat_soc": "64"})
    client.attributes["sensor.bat_power"] = {"unit_of_measurement": "kW"}
    client.apply_services = True
    for entity, (state, attrs) in states.items():
        client.set_state(entity.replace("__", "."), state, **attrs)
    return client


def _battery(client: FakeHAClient, setpoint: dict | None):
    caps = dict(READINGS)
    if setpoint is not None:
        caps["power_setpoint"] = setpoint
    return registry.build_device("bat", {
        "type": "homeassistant_battery", "category": "battery",
        "capabilities": caps, **PARAMS,
    }, BuildContext(backends=BackendsConfig(), ha_client=client))


def _cmd(value: float, behaviour: Behaviour | None = None) -> DeviceCommand:
    return DeviceCommand(device_id="bat", command="set_power_w", value=value,
                         behaviour=behaviour)


def _writes(client: FakeHAClient) -> list[tuple]:
    out = []
    for _domain, service, data in client.calls:
        value = data.get("value", data.get("option", data.get("power_w")))
        out.append((service, data.get("entity_id"), value))
    return out


async def test_readings_and_read_only_battery() -> None:
    client = _client()
    dev = _battery(client, None)
    st = await dev.get_state()
    assert (st.power_w, st.soc_pct, st.available) == (-1200.0, 64.0, True)
    await dev.send_command(_cmd(500))
    assert client.calls == [] and dev.supported_behaviours is None
    client.set_state("sensor.bat_soc", "unavailable")
    assert not (await dev.get_state()).available


async def test_a_signed_number_in_kw_with_deadband() -> None:
    client = _client(number__bat_setpoint=("0", {"unit_of_measurement": "kW",
                                                  "min": -5, "max": 5, "step": 0.01}))
    dev = _battery(client, {"control": "number", "number": "number.bat_setpoint"})
    await dev.send_command(_cmd(1500))
    await dev.send_command(_cmd(1510))          # within the 25 W deadband
    await dev.send_command(_cmd(-2000))
    assert _writes(client) == [("set_value", "number.bat_setpoint", 1.5),
                               ("set_value", "number.bat_setpoint", -2.0)]
    st = await dev.get_state()
    assert st.extra["actuators"][0]["followed"] is True


async def test_a_inverted_sign() -> None:
    client = _client(number__p=("0", {}))
    dev = _battery(client, {"control": "number", "number": "number.p", "invert_sign": True})
    await dev.send_command(_cmd(800))
    assert _writes(client) == [("set_value", "number.p", -800.0)]


async def test_b_charge_and_discharge_numbers() -> None:
    client = _client(number__cha=("0", {}), number__dis=("0", {}))
    dev = _battery(client, {"control": "charge_discharge",
                            "charge": "number.cha", "discharge": "number.dis"})
    await dev.send_command(_cmd(1200))
    await dev.send_command(_cmd(-700))
    await dev.send_command(_cmd(0))
    assert _writes(client) == [
        ("set_value", "number.cha", 1200.0),
        ("set_value", "number.cha", 0.0), ("set_value", "number.dis", 700.0),
        ("set_value", "number.dis", 0.0),
    ]


async def test_c_mode_and_power_by_sign_and_release() -> None:
    options = ["Forcible charge", "Forcible discharge", "Fixed", "Self consumption"]
    client = _client(select__mode=("Self consumption", {"options": options}),
                     number__power=("0", {"unit_of_measurement": "W"}))
    dev = _battery(client, {
        "control": "mode_power", "mode": "select.mode", "power": "number.power",
        "charge_option": "Forcible charge", "discharge_option": "Forcible discharge",
        "hold_option": "Fixed", "release_option": "Self consumption",
    })
    # Setpoint control: the behaviour is ignored, the sign decides.
    await dev.send_command(_cmd(2000, B.ABSORB_SURPLUS))
    await dev.send_command(_cmd(-900, B.SELF_CONSUMPTION))
    await dev.send_command(_cmd(0))
    await dev.release()
    assert _writes(client) == [
        ("set_value", "number.power", 2000.0), ("select_option", "select.mode", "Forcible charge"),
        ("set_value", "number.power", 900.0), ("select_option", "select.mode", "Forcible discharge"),
        ("select_option", "select.mode", "Fixed"),
        ("select_option", "select.mode", "Self consumption"),
    ]
    assert dev.supported_behaviours is None


async def test_c_mode_with_separate_charge_and_discharge_power() -> None:
    options = ["Forcible charge", "Forcible discharge", "Fixed"]
    client = _client(select__mode=("Fixed", {"options": options}),
                     number__cha=("0", {"unit_of_measurement": "W"}),
                     number__dis=("0", {"unit_of_measurement": "kW"}))
    dev = _battery(client, {
        "control": "mode_power", "mode": "select.mode",
        "charge_power": "number.cha", "discharge_power": "number.dis",
        "charge_option": "Forcible charge", "discharge_option": "Forcible discharge",
        "hold_option": "Fixed",
    })
    await dev.send_command(_cmd(2000))
    await dev.send_command(_cmd(-900))
    await dev.send_command(_cmd(0))
    assert _writes(client) == [
        ("set_value", "number.cha", 2000.0), ("select_option", "select.mode", "Forcible charge"),
        ("set_value", "number.dis", 0.9), ("select_option", "select.mode", "Forcible discharge"),
        ("select_option", "select.mode", "Fixed"),
    ]


def test_c_mode_power_one_or_two_numbers() -> None:
    base = {"control": "mode_power", "mode": "select.mode", "charge_option": "a",
            "discharge_option": "b", "hold_option": "c"}
    with pytest.raises(DeviceConfigError, match="not both"):
        _battery(_client(), {**base, "power": "number.p", "charge_power": "number.c",
                             "discharge_power": "number.d"})
    with pytest.raises(DeviceConfigError, match="needs 'discharge_power'"):
        _battery(_client(), {**base, "charge_power": "number.c"})
    # Validation: the form's switch follows the keys that are set.
    def errors(setpoint: dict) -> list[str]:
        cfg = {"category": "battery", "type": "homeassistant_battery",
               "capabilities": {**READINGS, "power_setpoint": setpoint}, "params": PARAMS}
        try:
            validate_plugins(parse_devices({"bat": cfg}))
        except DeviceConfigError as exc:
            return exc.errors
        return []

    assert errors({**base, "charge_power": "number.c", "discharge_power": "number.d"}) == []
    assert errors({**base, "power": "number.p"}) == []
    assert [e for e in errors({**base, "charge_power": "number.c"})
            if "required" in e] == [(
        "device 'bat': capabilities.power_setpoint: key 'discharge_power' is required "
        "(Number entity that takes the power while discharging, in W or kW, always positive)")]


async def test_d_action_with_data_deadband_and_refresh() -> None:
    client = _client()
    dev = _battery(client, {"control": "action", "action": "script.set_battery",
                            "data": {"device": "x"}, "power_key": "watts"})
    action = dev.control.actuators[0]
    now = [0.0]
    action._clock = lambda: now[0]
    await dev.send_command(_cmd(1000))
    now[0] = 10
    await dev.send_command(_cmd(1010))       # within the deadband, recent: skipped
    now[0] = 70
    await dev.send_command(_cmd(1010))       # refresh after 60 s
    assert client.calls == [("script", "set_battery", {"device": "x", "watts": 1000.0}),
                            ("script", "set_battery", {"device": "x", "watts": 1010.0})]


def _mode_battery(client: FakeHAClient):
    return _battery(client, {
        "control": "modes", "mode": "select.mode",
        "modes": {
            "charge": {"option": "Forcible charge", "power": "number.cha"},
            "discharge": {"option": "Forcible discharge", "power": "number.dis"},
            "self_consumption": {"option": "Self consumption",
                                 "set": {"number.max_dis": 5000}},
            "absorb_surplus": {"option": "Self consumption", "set": {"number.max_dis": 0}},
        },
    })


def _mode_client() -> FakeHAClient:
    options = ["Forcible charge", "Forcible discharge", "Self consumption"]
    return _client(select__mode=("Forcible charge", {"options": options}),
                   number__cha=("0", {}), number__dis=("0", {}), number__max_dis=("5000", {}))


async def test_mode_control_follows_the_behaviour() -> None:
    client = _mode_client()
    dev = _mode_battery(client)
    assert dev.supported_behaviours == {B.CHARGE, B.DISCHARGE, B.SELF_CONSUMPTION,
                                        B.ABSORB_SURPLUS}
    await dev.send_command(_cmd(350, B.ABSORB_SURPLUS))
    await dev.send_command(_cmd(-600, B.SELF_CONSUMPTION))
    await dev.send_command(_cmd(3000, B.CHARGE))
    assert _writes(client) == [
        ("set_value", "number.max_dis", 0.0), ("select_option", "select.mode", "Self consumption"),
        ("set_value", "number.max_dis", 5000.0),       # same option: only the limit changes
        ("set_value", "number.cha", 3000.0), ("select_option", "select.mode", "Forcible charge"),
    ]
    await dev.release()                                  # default: self_consumption
    assert client._states["select.mode"] == "Self consumption"


async def test_mode_control_through_the_control_loop_with_fallback() -> None:
    """The loop sends hold, which this battery has no mode for: it falls back
    to the forced mode of the watt value."""
    client = _mode_client()
    dev = _mode_battery(client)
    reg = DeviceRegistry()
    reg.register(dev)
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(StorageConstraints(
        device_id="bat", capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5)))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="bat", timestep=now, power_kw=-2.0)]))
    live = LiveSituation(
        timestamp=now, grid_power_w=900.0, dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=60)},
        current_price_eur_per_kwh=0.3, pv_opportunity_price_eur_per_kwh=0.08,
    )
    await loop.tick(live, reg)
    assert loop.last_behaviours["bat"] is B.SELF_CONSUMPTION
    assert client._states["select.mode"] == "Self consumption"


def test_validation_of_the_power_setpoint() -> None:
    def errors(setpoint: dict) -> list[str]:
        cfg = {"bat": {"category": "battery", "type": "homeassistant_battery",
                       "capabilities": {**READINGS, "power_setpoint": setpoint},
                       "params": PARAMS}}
        try:
            validate_plugins(parse_devices(cfg))
        except DeviceConfigError as exc:
            return exc.errors
        return []

    assert errors({"control": "number", "number": "number.p"}) == []
    assert errors({"control": "number", "number": "sensor.p"}) == [(
        "device 'bat': capabilities.power_setpoint: number must be a number / input_number "
        "entity, got 'sensor.p'"
    )]
    assert errors({"control": "charge_discharge", "charge": "number.c"}) == [(
        "device 'bat': capabilities.power_setpoint: key 'discharge' is required "
        "(Number entity that takes the discharge power, in W or kW)"
    )]
    assert "must be one of" in errors({"control": "magic"})[0]


def test_build_errors_of_mode_control() -> None:
    client = _mode_client()
    with pytest.raises(DeviceConfigError, match="not a behaviour"):
        _battery(client, {"control": "modes", "mode": "select.mode",
                          "modes": {"turbo": {"option": "x"}}})
    with pytest.raises(DeviceConfigError, match="has no mode"):
        _battery(client, {"control": "modes", "mode": "select.mode", "release": "hold",
                          "modes": {"charge": {"option": "x"}}})
    with pytest.raises(DeviceConfigError, match="needs soc_pct"):
        registry.build_device("bat", {"type": "homeassistant_battery", "category": "battery",
                                      "capabilities": {"power_w": {"entity": "sensor.p"}}},
                              BuildContext(backends=BackendsConfig(), ha_client=client))


async def test_mode_control_fallbacks() -> None:
    options = ["Forcible charge", "Self consumption"]
    client = _client(select__mode=("Self consumption", {"options": options}),
                     number__cha=("500", {}))
    dev = _battery(client, {"control": "modes", "mode": "select.mode", "modes": {
        "charge": {"option": "Forcible charge", "power": "number.cha"},
        "self_consumption": {"option": "Self consumption"}}})
    # hold → no absorb_surplus mode → the charge mode at 0 W
    await dev.send_command(_cmd(0, B.HOLD))
    assert (client._states["select.mode"], client._states["number.cha"]) == (
        "Forcible charge", "0.0")
    # discharge → self_consumption
    await dev.send_command(_cmd(-900, B.DISCHARGE))
    assert client._states["select.mode"] == "Self consumption"
    # absorb_surplus → self_consumption
    await dev.send_command(_cmd(300, B.ABSORB_SURPLUS))
    assert client._states["select.mode"] == "Self consumption"


async def test_mode_control_without_any_fitting_mode_warns_once(caplog) -> None:
    import logging

    client = _client(select__mode=("A", {"options": ["A"]}))
    dev = _battery(client, {"control": "modes", "mode": "select.mode",
                            "modes": {"self_consumption": {"option": "A"}}})
    with caplog.at_level(logging.WARNING):
        await dev.send_command(_cmd(1000, B.CHARGE))   # charge → absorb → self_consumption
        assert client._states["select.mode"] == "A"
        dev2 = _battery(client, {"control": "modes", "mode": "select.mode",
                                 "modes": {"discharge": {"option": "A"}}})
        await dev2.send_command(_cmd(0, B.HOLD))
        await dev2.send_command(_cmd(0, B.HOLD))
    assert sum("no mode for hold" in r.getMessage() for r in caplog.records) == 1


async def test_modes_share_one_actuator_per_entity() -> None:
    """absorb_surplus sets the discharge limit to 0, self_consumption back to
    5000, then absorb_surplus again within seconds: the 0 must be written."""
    client = _mode_client()
    dev = _mode_battery(client)
    for value, behaviour in ((300, B.ABSORB_SURPLUS), (-600, B.SELF_CONSUMPTION),
                             (300, B.ABSORB_SURPLUS)):
        await dev.send_command(_cmd(value, behaviour))
    assert client._states["number.max_dis"] == "0.0"
    assert len({a.entity_id for a in dev.control.actuators}) == len(dev.control.actuators)

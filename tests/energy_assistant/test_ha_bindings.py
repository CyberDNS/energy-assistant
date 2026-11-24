"""Shared Home Assistant layer: read bindings, actuators, homeassistant_sensor."""

from __future__ import annotations

import logging

import pytest
from helpers.fake_ha_client import FakeHAClient

from energy_assistant.core.config import BackendsConfig
from energy_assistant.core.device_model import (
    Capability,
    DeviceCategory,
    DeviceConfigError,
    parse_devices,
)
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.loader.device_loader import validate_plugins
from energy_assistant.plugins import registry
from energy_assistant.plugins._homeassistant.actuators import (
    NumberActuator,
    SelectActuator,
    SwitchActuator,
)
from energy_assistant.plugins._homeassistant.bindings import (
    ValueKind,
    parse_binding,
)
from energy_assistant.plugins.homeassistant_sensor.device import HASensorDevice

# ── read bindings ────────────────────────────────────────────────────────────


def _binding(cap: Capability, **raw):
    return parse_binding(f"test {cap.value}", cap, raw)


async def test_numbers_are_converted_from_the_entity_unit() -> None:
    client = FakeHAClient()
    client.set_state("sensor.p_kw", "1.5", unit_of_measurement="kW")
    client.set_state("sensor.e_wh", "2500", unit_of_measurement="Wh")
    client.set_state("sensor.t_f", "68", unit_of_measurement="°F")
    client.set_state("sensor.range_mi", "100", unit_of_measurement="mi")
    client.set_state("sensor.plain", "42")
    assert (await _binding(Capability.POWER_W, entity="sensor.p_kw").read(client)).value == 1500.0
    assert (await _binding(Capability.ENERGY_IN_KWH, entity="sensor.e_wh").read(client)).value == 2.5
    assert (await _binding(Capability.TEMPERATURE_C, entity="sensor.t_f").read(client)).value == 20.0
    assert (await _binding(Capability.RANGE_KM, entity="sensor.range_mi").read(client)
            ).value == pytest.approx(160.9344)
    assert (await _binding(Capability.SOC_PCT, entity="sensor.plain").read(client)).value == 42.0


async def test_invert_scale_pair_phases_and_attribute() -> None:
    client = FakeHAClient({"sensor.pv": "2036", "sensor.i": "500", "sensor.e": "200",
                           "sensor.l1": "100", "sensor.l2": "200", "sensor.l3": "300",
                           "sensor.soc": "655"})
    client.set_state("climate.living", "heat", current_temperature=21.5, hvac_action="idle")
    pv = _binding(Capability.POWER_W, entity="sensor.pv", invert_sign=True)
    assert (await pv.read(client)).value == -2036.0
    grid = _binding(Capability.POWER_W, import_entity="sensor.i", export_entity="sensor.e")
    assert (await grid.read(client)).value == 300.0
    assert await grid.read_pair(client) == (500.0, 200.0)
    phases = _binding(Capability.PHASE_POWER_W,
                      entities=["sensor.l1", "sensor.l2", "sensor.l3"])
    assert (await phases.read(client)).value == [100.0, 200.0, 300.0]
    soc = _binding(Capability.SOC_PCT, entity="sensor.soc", scale=0.1)
    assert (await soc.read(client)).value == pytest.approx(65.5)
    temp = _binding(Capability.TEMPERATURE_C, entity="climate.living",
                    attribute="current_temperature")
    assert (await temp.read(client)).value == 21.5


async def test_on_off_states() -> None:
    client = FakeHAClient({"binary_sensor.plug": "on", "sensor.status": "Charging",
                           "device_tracker.car": "home", "binary_sensor.gone": "unavailable"})
    assert (await _binding(Capability.PLUGGED, entity="binary_sensor.plug").read(client)).value
    assert (await _binding(Capability.LOCATION_HOME, entity="device_tracker.car")
            .read(client)).value
    status = _binding(Capability.PLUGGED, entity="sensor.status", on=["Charging", "Wait car"])
    assert (await status.read(client)).value is True
    client.set_state("sensor.status", "Idle")
    assert (await status.read(client)).value is False
    gone = _binding(Capability.PLUGGED, entity="binary_sensor.gone")
    assert (await gone.read(client)).value is None


async def test_unavailable_and_missing_entities(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeHAClient({"sensor.p": "unavailable", "sensor.i": "5"})
    assert not (await _binding(Capability.POWER_W, entity="sensor.p").read(client)).available
    pair = _binding(Capability.POWER_W, import_entity="sensor.i", export_entity="sensor.nope")
    with caplog.at_level(logging.WARNING):
        assert (await pair.read(client)).value is None
        assert (await pair.read(client)).value is None
    missing = [r for r in caplog.records if "not found in Home Assistant" in r.message]
    assert len(missing) == 1 and "sensor.nope" in missing[0].message  # logged once


def test_binding_parse_errors() -> None:
    with pytest.raises(DeviceConfigError, match="not both"):
        _binding(Capability.POWER_W, entity="sensor.a", import_entity="sensor.b",
                 export_entity="sensor.c")
    with pytest.raises(DeviceConfigError, match="needs 'export_entity'"):
        _binding(Capability.POWER_W, import_entity="sensor.b")
    with pytest.raises(DeviceConfigError, match="unknown key"):
        _binding(Capability.SOC_PCT, entity="sensor.a", invert_sign=True)
    with pytest.raises(DeviceConfigError, match="needs 'entity"):
        _binding(Capability.SOC_PCT)
    assert _binding(Capability.SOC_PCT, entity="sensor.a").kind is ValueKind.PERCENT


def test_validation_checks_domains_and_conditional_keys() -> None:
    def errors(cfg: dict) -> list[str]:
        try:
            validate_plugins(parse_devices(cfg))
        except DeviceConfigError as exc:
            return exc.errors
        return []

    plug = {"category": "switch", "type": "homeassistant_switch",
            "capabilities": {"switch": {"entity": "sensor.not_a_switch"}}}
    assert errors({"plug": plug}) == [(
        "device 'plug': capabilities.switch: entity must be a switch / input_boolean / "
        "light / fan entity, got 'sensor.not_a_switch'"
    )]
    meter = {"category": "meter", "type": "homeassistant_sensor",
             "capabilities": {"power_w": {"import_entity": "sensor.i"}}}
    assert errors({"grid": meter}) == [(
        "device 'grid': capabilities.power_w: key 'export_entity' is required "
        "(Export power sensor)"
    )]
    meter["capabilities"]["power_w"]["export_entity"] = "sensor.e"
    assert errors({"grid": meter}) == []
    # Keys of one capability are not accepted for another.
    meter["capabilities"]["energy_in_kwh"] = {"entity": "sensor.ein", "invert_sign": True}
    assert errors({"grid": meter}) == [(
        "device 'grid': capabilities.energy_in_kwh: unknown key 'invert_sign' (allowed: "
        "entity, import_entity, export_entity, attribute, scale)"
    )]


def test_bound_entity_only_for_a_plain_state() -> None:
    specs = parse_devices({
        "a": {"category": "consumer", "type": "homeassistant_sensor",
              "capabilities": {"power_w": {"entity": "sensor.a"}}},
        "b": {"category": "consumer", "type": "homeassistant_sensor",
              "capabilities": {"power_w": {"entity": "sensor.b", "scale": 1000}}},
        "c": {"category": "sensor", "type": "homeassistant_sensor",
              "capabilities": {"temperature_c": {"entity": "climate.c",
                                                 "attribute": "current_temperature"}}},
    })
    assert specs["a"].bound_entity(Capability.POWER_W) == "sensor.a"
    assert specs["b"].bound_entity(Capability.POWER_W) is None
    assert specs["c"].bound_entity(Capability.TEMPERATURE_C) is None


# ── actuators ────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _with_clock(actuator):
    clock = _Clock()
    actuator._clock = clock
    return actuator, clock


async def test_switch_reconciles_and_rate_limits() -> None:
    client = FakeHAClient({"switch.c": "off"})
    switch, clock = _with_clock(SwitchActuator("dev", client, "switch.c", min_interval_s=5))
    assert await switch.apply(True)
    assert client.calls == [("homeassistant", "turn_on", {"entity_id": "switch.c"})]
    # HA has not switched yet: no second write within min_interval_s …
    clock.now += 2
    assert not await switch.apply(True)
    # … but again after it (the device reverted, or the write was lost).
    clock.now += 4
    assert await switch.apply(True)
    client.set_state("switch.c", "on")
    assert not await switch.apply(True)
    assert len(client.calls) == 2 and switch.health.followed


async def test_actuator_reports_a_value_that_is_not_followed(
        caplog: pytest.LogCaptureFixture) -> None:
    client = FakeHAClient({"switch.c": "off"})
    switch, clock = _with_clock(SwitchActuator("dev", client, "switch.c",
                                               min_interval_s=5, follow_timeout_s=60))
    await switch.apply(True)
    clock.now += 30
    await switch.apply(True)
    assert switch.health.followed  # not yet: within the timeout
    clock.now += 31
    with caplog.at_level(logging.WARNING):
        await switch.apply(True)
        clock.now += 10
        await switch.apply(True)
    assert not switch.health.followed
    assert switch.health.to_dict() == {"entity_id": "switch.c", "desired": True,
                                       "current": False, "followed": False}
    assert len([r for r in caplog.records if "does not follow" in r.message]) == 1
    client.set_state("switch.c", "on")
    await switch.apply(True)
    assert switch.health.followed


async def test_number_is_clamped_rounded_and_compared_with_tolerance() -> None:
    client = FakeHAClient()
    client.set_state("number.current", "10", min=6, max=16, step=1)
    number, _ = _with_clock(NumberActuator("dev", client, "number.current", tolerance=0.5))
    assert await number.apply(20)
    assert client.calls[-1] == ("number", "set_value",
                                {"entity_id": "number.current", "value": 16.0})
    client.set_state("number.current", "16", min=6, max=16, step=1)
    assert not await number.apply(15.7)  # rounds to 16 → already there
    assert await number.apply(7.2)
    assert client.calls[-1][2]["value"] == 7.0
    assert await NumberActuator("dev", client, "input_number.x", maximum=5).apply(9)
    assert client.calls[-1] == ("input_number", "set_value",
                                {"entity_id": "input_number.x", "value": 5.0})


async def test_select_writes_options_and_warns_about_unknown_ones(
        caplog: pytest.LogCaptureFixture) -> None:
    client = FakeHAClient()
    client.set_state("select.mode", "Stop", options=["Stop", "PV", "Instant"])
    select = SelectActuator("dev", client, "select.mode")
    assert await select.apply("PV")
    assert client.calls[-1] == ("select", "select_option",
                                {"entity_id": "select.mode", "option": "PV"})
    client.set_state("select.mode", "PV", options=["Stop", "PV", "Instant"])
    assert not await select.apply("PV")
    other = SelectActuator("dev", client, "select.mode")
    with caplog.at_level(logging.WARNING):
        await other.apply("Turbo")
    assert any("not an option" in r.message for r in caplog.records)


async def test_unreadable_entity_is_written_anyway() -> None:
    client = FakeHAClient()  # entity unknown to HA
    assert await SwitchActuator("dev", client, "switch.x").apply(False)
    assert client.calls == [("homeassistant", "turn_off", {"entity_id": "switch.x"})]


# ── homeassistant_sensor ─────────────────────────────────────────────────────


def _build(device_id: str, cfg: dict, client: FakeHAClient):
    return registry.build_device(device_id, cfg,
                                 BuildContext(backends=BackendsConfig(), ha_client=client))


async def test_sensor_device_reads_every_bound_capability() -> None:
    client = FakeHAClient({"sensor.i": "1.2", "sensor.e": "0.2", "sensor.ein": "1234.5",
                           "sensor.eout": "99", "sensor.l1": "1", "sensor.l2": "2",
                           "sensor.l3": "3"})
    client.attributes["sensor.i"] = client.attributes["sensor.e"] = {"unit_of_measurement": "kW"}
    dev = _build("grid", {
        "type": "homeassistant_sensor", "category": "meter",
        "capabilities": {
            "power_w": {"import_entity": "sensor.i", "export_entity": "sensor.e"},
            "energy_in_kwh": {"entity": "sensor.ein"},
            "energy_out_kwh": {"entity": "sensor.eout"},
            "phase_power_w": {"entities": ["sensor.l1", "sensor.l2", "sensor.l3"]},
        },
    }, client)
    assert isinstance(dev, HASensorDevice) and dev.category is DeviceCategory.METER
    st = await dev.get_state()
    assert st.available and st.power_w == pytest.approx(1000.0)
    assert st.energy_kwh == 1234.5
    assert st.extra == {"import_w": 1200.0, "export_w": 200.0, "energy_in_kwh": 1234.5,
                        "energy_out_kwh": 99.0, "phase_power_w": [1.0, 2.0, 3.0]}


async def test_temperature_sensor_and_the_old_type_name() -> None:
    client = FakeHAClient({"sensor.outdoor": "4.5"})
    dev = _build("outdoor", {"type": "generic_homeassistant", "category": "sensor",
                             "capabilities": {"temperature_c": {"entity": "sensor.outdoor"}}},
                 client)
    st = await dev.get_state()
    assert st.available and st.power_w is None
    assert st.extra == {"temperature_c": 4.5, "measured_value": 4.5}
    client.set_state("sensor.outdoor", "unavailable")
    assert not (await dev.get_state()).available
    info = registry.device_info("generic_homeassistant")
    assert info.legacy and not registry.device_info("homeassistant_sensor").legacy
    schema = registry.device_info("homeassistant_sensor").to_dict()
    assert "import_entity" in schema["bindings"]["power_w"]
    assert "import_entity" not in schema["bindings"]["temperature_c"]
    assert schema["bindings"]["power_w"]["entity"]["domains"][0] == "sensor"


def test_sensor_device_needs_a_binding_and_rejects_old_keys() -> None:
    with pytest.raises(DeviceConfigError, match="generic_consumer"):
        _build("c", {"type": "homeassistant_sensor", "category": "consumer"}, FakeHAClient())
    with pytest.raises(DeviceConfigError, match="moved to"):
        _build("c", {"type": "generic_homeassistant", "category": "consumer",
                     "power": "sensor.x"}, FakeHAClient())

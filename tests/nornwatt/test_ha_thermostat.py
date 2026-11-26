"""homeassistant_thermostat: a thermal load that is its own switch (#106 phase 6)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from helpers.fake_ha_client import FakeHAClient

from nornwatt.assets.threshold import ThresholdControlContributor
from nornwatt.core.composition import threshold_constraints
from nornwatt.core.config import BackendsConfig
from nornwatt.core.control import ControlLoop, LiveSituation
from nornwatt.core.device_model import DeviceConfigError, parse_devices
from nornwatt.core.ledger import BatteryCostLedger
from nornwatt.core.models import ControlIntent, DeviceCommand, EnergyPlan
from nornwatt.core.plugin_registry import BuildContext
from nornwatt.core.registry import DeviceRegistry
from nornwatt.loader.device_loader import validate_plugins
from nornwatt.plugins import registry

HEATER = {"bottom_threshold": 45.0, "top_threshold": 55.0, "direction": "increases",
          "rated_power_w": 1800, "active_rate_per_h": 8.0, "drift_rate_per_h": 0.6}
COOLER = {**HEATER, "bottom_threshold": 18.0, "top_threshold": 20.5, "direction": "reduces"}


def _client() -> FakeHAClient:
    client = FakeHAClient()
    client.apply_services = True
    client.set_state("climate.tank", "heat", current_temperature=48.5, temperature=50,
                     hvac_action="idle", preset_mode="eco")
    client.set_state("water_heater.tank", "eco", current_temperature=48.5, temperature=50,
                     operation_mode="eco")
    return client


def _thermostat(client: FakeHAClient, setpoint: dict, params: dict = HEATER, **caps):
    return registry.build_device("tank", {
        "type": "homeassistant_thermostat", "category": "thermal_load", **params,
        "capabilities": {"temperature_c": {"entity": setpoint["entity"],
                                           "attribute": "current_temperature"},
                         "power_setpoint": setpoint, **caps},
    }, BuildContext(backends=BackendsConfig(), ha_client=client))


def _cmd(run: bool) -> DeviceCommand:
    return DeviceCommand(device_id="tank", command="set_power_w", value=1800.0 if run else 0.0)


def _climate_calls(client: FakeHAClient) -> list[tuple]:
    return [(service, {k: v for k, v in data.items() if k != "entity_id"})
            for _d, service, data in client.calls]


async def test_target_temperature_defaults_for_a_heater_and_a_cooler() -> None:
    client = _client()
    heater = _thermostat(client, {"control": "target_temperature", "entity": "climate.tank"})
    await heater.send_command(_cmd(True))     # runs towards the top threshold
    await heater.send_command(_cmd(False))    # stops at the bottom one
    cooler = _thermostat(client, {"control": "target_temperature", "entity": "climate.tank",
                                  "run_temperature": 17.5}, COOLER)
    await cooler.send_command(_cmd(True))
    await cooler.send_command(_cmd(False))
    assert _climate_calls(client) == [
        ("set_temperature", {"temperature": 55.0}), ("set_temperature", {"temperature": 45.0}),
        ("set_temperature", {"temperature": 17.5}), ("set_temperature", {"temperature": 20.5}),
    ]


@pytest.mark.parametrize(("setpoint", "expected"), [
    ({"control": "hvac_mode", "entity": "climate.tank", "run_mode": "heat", "stop_mode": "off"},
     [("set_hvac_mode", {"hvac_mode": "off"})]),       # already "heat": only stop writes
    ({"control": "preset", "entity": "climate.tank", "run_preset": "boost", "stop_preset": "eco"},
     [("set_preset_mode", {"preset_mode": "boost"}),
      ("set_preset_mode", {"preset_mode": "eco"})]),
    ({"control": "water_heater", "entity": "water_heater.tank", "run_temperature": 60},
     [("set_temperature", {"temperature": 60.0}), ("set_temperature", {"temperature": 45.0})]),
    ({"control": "water_heater_operation", "entity": "water_heater.tank",
      "run_operation": "high_demand", "stop_operation": "eco"},
     [("set_operation_mode", {"operation_mode": "high_demand"}),
      ("set_operation_mode", {"operation_mode": "eco"})]),
])
async def test_variants(setpoint, expected) -> None:
    client = _client()
    dev = _thermostat(client, setpoint)
    await dev.send_command(_cmd(True))
    await dev.send_command(_cmd(False))
    assert _climate_calls(client) == expected


async def test_state_from_hvac_action_or_the_command() -> None:
    client = _client()
    dev = _thermostat(client, {"control": "target_temperature", "entity": "climate.tank"})
    st = await dev.get_state()
    assert (st.available, st.power_w, st.extra["measured_value"], st.extra["running"]) == (
        True, 0.0, 48.5, False)
    client.set_state("climate.tank", "heat", current_temperature=49.0, hvac_action="heating")
    st = await dev.get_state()
    assert (st.power_w, st.extra["running"], st.extra["hvac_action"]) == (1800.0, True, "heating")
    # A power sensor wins over the rated power.
    metered = _thermostat(client, {"control": "target_temperature", "entity": "climate.tank"},
                          power_w={"entity": "sensor.tank_power"})
    client.set_state("sensor.tank_power", "1620")
    assert (await metered.get_state()).power_w == 1620.0
    # No hvac_action (a water heater): running is what it was told.
    wh = _thermostat(client, {"control": "water_heater", "entity": "water_heater.tank"})
    assert (await wh.get_state()).power_w == 0.0
    await wh.send_command(_cmd(True))
    assert (await wh.get_state()).power_w == 1800.0
    client.set_state("water_heater.tank", "eco", current_temperature=None)
    assert not (await wh.get_state()).available


async def test_normal_operation_restored_when_stale_and_on_release(caplog) -> None:
    client = _client()
    dev = _thermostat(client, {"control": "hvac_mode", "entity": "climate.tank",
                               "run_mode": "heat", "stop_mode": "off", "normal": "auto",
                               "stale_after_min": 30})
    now = [0.0]
    dev._clock = lambda: now[0]
    await dev.send_command(_cmd(False))
    assert client._states["climate.tank"] == "off"
    now[0] = 29 * 60
    await dev.get_state()
    assert client._states["climate.tank"] == "off"          # not stale yet
    now[0] = 31 * 60
    with caplog.at_level(logging.WARNING):
        await dev.get_state()
        await dev.get_state()
    assert client._states["climate.tank"] == "auto"
    assert sum("restored the normal operation" in r.getMessage() for r in caplog.records) == 1
    await dev.send_command(_cmd(True))
    assert client._states["climate.tank"] == "heat"
    await dev.release()
    assert client._states["climate.tank"] == "auto"


async def test_planned_through_the_thermal_load_contributor() -> None:
    client = _client()
    specs = parse_devices({"tank": {
        "category": "thermal_load", "type": "homeassistant_thermostat", "params": HEATER,
        "capabilities": {"temperature_c": {"entity": "climate.tank",
                                           "attribute": "current_temperature"},
                         "power_setpoint": {"control": "target_temperature",
                                            "entity": "climate.tank"}},
    }})
    validate_plugins(specs)
    (tc,) = threshold_constraints(specs)
    assert tc.rated_power_kw == 1.8
    dev = registry.build_device("tank", specs["tank"].plugin_config(),
                                BuildContext(backends=BackendsConfig(), ha_client=client))
    reg = DeviceRegistry()
    reg.register(dev)
    now = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(ThresholdControlContributor(tc))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="tank", timestep=now, power_kw=1.8)]))
    live = LiveSituation(timestamp=now, grid_power_w=0.0, dt_hours=0.0,
                         device_states={"tank": await dev.get_state()},
                         current_price_eur_per_kwh=0.3, pv_opportunity_price_eur_per_kwh=0.08)
    await loop.tick(live, reg)
    assert client.attributes["climate.tank"]["temperature"] == 55.0  # runs: towards the top
    assert _climate_calls(client) == [("set_temperature", {"temperature": 55.0})]


def test_build_errors() -> None:
    client = _client()
    with pytest.raises(DeviceConfigError, match="needs a climate entity"):
        _thermostat(client, {"control": "hvac_mode", "entity": "water_heater.tank",
                             "run_mode": "heat", "stop_mode": "off"})
    with pytest.raises(DeviceConfigError, match="needs 'run_preset' and 'stop_preset'"):
        _thermostat(client, {"control": "preset", "entity": "climate.tank"})
    with pytest.raises(DeviceConfigError, match="rated_power_w"):
        _thermostat(client, {"control": "target_temperature", "entity": "climate.tank"},
                    {k: v for k, v in HEATER.items() if k != "rated_power_w"})
    with pytest.raises(DeviceConfigError, match="remove 'controlled_by'"):
        registry.build_device("tank", {
            "type": "homeassistant_thermostat", "category": "thermal_load", **HEATER,
            "controlled_by": "plug",
            "capabilities": {"temperature_c": {"entity": "sensor.t"},
                             "power_setpoint": {"control": "target_temperature",
                                                "entity": "climate.tank"}},
        }, BuildContext(backends=BackendsConfig(), ha_client=client))


async def test_target_is_kept_within_the_entity_range(caplog) -> None:
    client = _client()
    client.set_state("climate.tank", "heat", current_temperature=30, temperature=30,
                     min_temp=7, max_temp=35)
    dev = _thermostat(client, {"control": "target_temperature", "entity": "climate.tank",
                               "stop_temperature": 25})
    with caplog.at_level(logging.WARNING):
        await dev.send_command(_cmd(True))        # top threshold 55 → 35
        await dev.send_command(_cmd(False))
        await dev.send_command(_cmd(True))
    assert [d["temperature"] for _s, d in _climate_calls(client)] == [35.0, 25.0, 35.0]
    assert sum("accepts 7–35 only" in r.getMessage() for r in caplog.records) == 1

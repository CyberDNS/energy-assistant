"""Unified device model: category schemas, validation, plugins, composition."""

from __future__ import annotations

import pytest

from energy_assistant.core.composition import compose, storage_constraints
from energy_assistant.core.config import AppConfig, BackendsConfig
from energy_assistant.core.device_model import (
    CATEGORY_SCHEMAS,
    Capability,
    DeviceCategory,
    DeviceConfigError,
    parse_devices,
)
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.loader.device_loader import build, validate_plugins
from energy_assistant.plugins import registry as plugin_registry

BATTERY = {
    "category": "battery", "type": "zendure", "cloud_key": "k",
    "product_key": "p", "device_key": "d",
    "params": {"capacity_kwh": 7.6, "max_charge_kw": 1.2, "max_discharge_kw": 0.8},
}
GRID = {"category": "grid_meter", "type": "tasmota", "host": "h"}


def _errors(raw: dict) -> list[str]:
    with pytest.raises(DeviceConfigError) as exc:
        parse_devices(raw)
    return exc.value.errors


def _plugin_errors(raw: dict) -> list[str]:
    with pytest.raises(DeviceConfigError) as exc:
        validate_plugins(parse_devices(raw))
    return exc.value.errors


# ── parsing ──────────────────────────────────────────────────────────────────


def test_spec_splits_model_keys_params_and_plugin_settings() -> None:
    spec = parse_devices({"bat": {**BATTERY, "label": "Zendure", "tariff": "household",
                                  "maintenance_charge_w": 300}})["bat"]
    assert spec.category is DeviceCategory.BATTERY
    assert spec.type == "zendure"
    assert spec.label == "Zendure" and spec.tariff == "household"
    assert spec.params == {"capacity_kwh": 7.6, "max_charge_kw": 1.2, "max_discharge_kw": 0.8}
    assert spec.settings == {"cloud_key": "k", "product_key": "p", "device_key": "d",
                             "maintenance_charge_w": 300}
    # The plugin factory still gets one flat dict.
    cfg = spec.plugin_config()
    assert cfg["capacity_kwh"] == 7.6 and cfg["cloud_key"] == "k"
    assert cfg["category"] == "battery" and cfg["type"] == "zendure"


def test_params_are_coerced_to_their_type() -> None:
    spec = parse_devices({"bat": {**BATTERY, "params": {**BATTERY["params"], "capacity_kwh": 7}}})
    assert isinstance(spec["bat"].params["capacity_kwh"], float)


def test_capability_bindings() -> None:
    spec = parse_devices({"pv": {
        "category": "pv", "type": "generic_homeassistant",
        "capabilities": {"power_w": {"entity": "sensor.pv", "invert_sign": True}},
    }})["pv"]
    assert spec.bound_entity(Capability.POWER_W) == "sensor.pv"
    assert spec.plugin_config()["capabilities"] == {
        "power_w": {"entity": "sensor.pv", "invert_sign": True},
    }


def test_to_dict_round_trips() -> None:
    raw = {"bat": {**BATTERY, "metered_by": "grid"}, "grid": GRID}
    specs = parse_devices(raw)
    again = parse_devices({k: {kk: vv for kk, vv in s.to_dict().items() if kk != "id"}
                           for k, s in specs.items()})
    assert again == specs


# ── validation messages ──────────────────────────────────────────────────────


def test_role_is_rejected_with_a_hint() -> None:
    assert _errors({"m": {"role": "meter", "type": "tasmota"}}) == [(
        "device 'm': 'role:' was replaced by 'category:' (one of grid_meter, meter, pv, "
        "battery, vehicle, charger, thermal_load, switch, consumer, sensor)"
    )]


def test_unknown_category_and_missing_type() -> None:
    assert _errors({"m": {"category": "smart_meter", "type": "x"}})[0].startswith(
        "device 'm': unknown category 'smart_meter'"
    )
    assert _errors({"m": {"category": "meter"}}) == ["device 'm': 'type:' (the plugin) is required"]


def test_required_params_and_misplaced_params() -> None:
    errors = _errors({"bat": {"category": "battery", "type": "zendure", "capacity_kwh": 7.6}})
    assert "device 'bat': params.capacity_kwh is required for category 'battery' " \
           "(Usable capacity (kWh))" in errors
    assert "device 'bat': 'capacity_kwh' is a model parameter — move it under 'params:'" in errors


def test_param_type_range_and_unknown() -> None:
    errors = _errors({"bat": {**BATTERY, "params": {
        **BATTERY["params"], "min_soc_pct": 120, "efficiency": "best", "cycle_life": "many",
        "colour": "red",
    }}})
    assert "device 'bat': params.min_soc_pct must be <= 100, got 120" in errors
    assert "device 'bat': params.efficiency must be one of configured, learned, got 'best'" in errors
    assert "device 'bat': params.cycle_life must be a int, got 'many'" in errors
    assert any(e.startswith("device 'bat': unknown param 'colour' for category 'battery'")
               for e in errors)


def test_capability_not_allowed_for_category() -> None:
    errors = _errors({"m": {**GRID, "capabilities": {"soc_pct": {"entity": "sensor.x"}}}})
    assert errors == [(
        "device 'm': capability 'soc_pct' is not allowed for category 'grid_meter' "
        "(allowed: energy_in_kwh, energy_out_kwh, phase_power_w, power_w)"
    )]
    assert "unknown capability 'watts'" in _errors(
        {"m": {**GRID, "capabilities": {"watts": {"entity": "s"}}}}
    )[0]


def test_connection_not_allowed_for_category() -> None:
    errors = _errors({"grid": {**GRID, "metered_by": "other"}})
    assert errors == ["device 'grid': category 'grid_meter' cannot have 'metered_by'"]


def test_connection_target_must_exist_and_have_the_right_category() -> None:
    errors = _errors({
        "grid": GRID,
        "bat": {**BATTERY, "metered_by": "nowhere"},
        "bat2": {**BATTERY, "metered_by": "bat"},
    })
    assert "device 'bat': metered_by 'nowhere' is not a configured device" in errors
    assert "device 'bat2': metered_by 'bat' is a 'battery', expected grid_meter, meter" in errors


def test_only_one_grid_meter() -> None:
    assert _errors({"a": GRID, "b": GRID}) == ["only one grid_meter is allowed, found: a, b"]


def test_metered_by_must_lead_to_the_grid_meter() -> None:
    errors = _errors({
        "grid": GRID,
        "sub": {"category": "meter", "type": "tasmota", "host": "h"},
        "bat": {**BATTERY, "metered_by": "sub"},
    })
    assert errors == [(
        "device 'bat': metered_by chain ends at 'sub', which is not the grid_meter "
        "(give it 'metered_by' too)"
    )]


def test_metered_by_loop_is_reported() -> None:
    errors = _errors({
        "grid": GRID,
        "a": {"category": "meter", "type": "tasmota", "host": "h", "metered_by": "b"},
        "b": {"category": "meter", "type": "tasmota", "host": "h", "metered_by": "a"},
    })
    assert "metered_by forms a loop: a → b → a" in errors


def test_thermal_load_needs_a_switch() -> None:
    base = {"category": "thermal_load", "type": "threshold_homeassistant",
            "capabilities": {"temperature_c": {"entity": "sensor.t"}},
            "params": {"rated_power_w": 150}}
    assert _errors({"aq": base}) == [(
        "device 'aq': a thermal_load needs 'controlled_by: <switch device>' "
        "(or a 'switch' capability binding)"
    )]
    plug = {"category": "switch", "type": "tasmota", "host": "h"}
    assert parse_devices({"aq": {**base, "controlled_by": "plug"}, "plug": plug})
    assert "controlled_by 'grid' is a 'grid_meter', expected switch" in _errors(
        {"aq": {**base, "controlled_by": "grid"}, "grid": GRID}
    )[0]


def test_all_errors_are_reported_together() -> None:
    errors = _errors({
        "a": {"role": "meter"},
        "b": {"category": "battery", "type": "zendure"},
    })
    assert len(errors) == 4  # a: role; b: three required params


def test_every_category_has_a_schema() -> None:
    assert set(CATEGORY_SCHEMAS) == set(DeviceCategory)
    for schema in CATEGORY_SCHEMAS.values():
        assert not schema.required_capabilities & schema.optional_capabilities


# ── plugin validation ────────────────────────────────────────────────────────


def test_unknown_plugin_type() -> None:
    errors = _plugin_errors({"m": {"category": "meter", "type": "smartmeter_xyz"}})
    assert errors[0].startswith("device 'm': unknown type 'smartmeter_xyz' (one of ")


def test_plugin_category_must_match() -> None:
    errors = _plugin_errors({"z": {**BATTERY, "category": "meter", "params": {}}})
    assert errors == ["device 'z': plugin 'zendure' cannot be a 'meter' (allowed: battery)"]


def test_plugin_must_be_able_to_bind_the_capability() -> None:
    errors = _plugin_errors({"m": {**GRID, "capabilities": {"power_w": {"entity": "s"}}}})
    assert errors == [
        "device 'm': plugin 'tasmota' cannot bind capability 'power_w' (bindable: none)"
    ]


def test_required_capability_must_be_provided() -> None:
    errors = _plugin_errors({"pv": {"category": "pv", "type": "generic_homeassistant"}})
    assert errors == [(
        "device 'pv': category 'pv' requires capability 'power_w', which plugin "
        "'generic_homeassistant' does not provide — bind it under "
        "'capabilities: {power_w: {entity: …}}'"
    )]


def test_live_style_config_validates() -> None:
    specs = parse_devices({
        "main_grid_meter": {**GRID, "tariff": "grid"},
        "household_meter": {"category": "meter", "type": "tibber_local", "host": "h",
                            "password": "x", "metered_by": "main_grid_meter"},
        "pv_production": {"category": "pv", "type": "generic_homeassistant",
                          "metered_by": "household_meter",
                          "capabilities": {"power_w": {"entity": "sensor.pv",
                                                       "invert_sign": True}}},
        "heatpump_meter": {"category": "consumer", "type": "differential",
                           "minuend": "main_grid_meter", "subtrahend": "household_meter"},
        "baseline": {"category": "consumer", "type": "generic_consumer"},
        "zendure": {**BATTERY, "metered_by": "household_meter"},
        "cp5": {"category": "charger", "type": "openwb_mqtt", "chargepoint_id": 5},
        "aquarium_cooler": {
            "category": "thermal_load", "type": "threshold_homeassistant",
            "capabilities": {"temperature_c": {"entity": "sensor.t"},
                             "switch": {"entity": "switch.c"}},
            "params": {"rated_power_w": 150},
        },
    })
    validate_plugins(specs)


# ── building ─────────────────────────────────────────────────────────────────


class _FakeHA:
    async def get_state(self, entity_id: str):  # pragma: no cover - not polled here
        return None


def test_build_uses_categories_and_bindings() -> None:
    cfg = AppConfig(devices=parse_devices({
        "pv": {"category": "pv", "type": "generic_homeassistant",
               "capabilities": {"power_w": {"entity": "sensor.pv", "invert_sign": True}}},
        "aq": {"category": "thermal_load", "type": "threshold_homeassistant",
               "capabilities": {"temperature_c": {"entity": "sensor.t"},
                                "switch": {"entity": "switch.c"}},
               "params": {"rated_power_w": 150}},
    }))
    ctx = BuildContext(backends=BackendsConfig(), ha_client=_FakeHA())
    registry, _, topology = build(cfg, ctx=ctx)
    pv, aq = registry.get("pv"), registry.get("aq")
    assert pv.category is DeviceCategory.PV
    assert pv._entity_power == "sensor.pv" and pv._invert_sign is True
    assert aq.category is DeviceCategory.THERMAL_LOAD
    assert (aq._entity_sensor, aq._entity_switch) == ("sensor.t", "switch.c")
    assert topology is None  # no grid meter


def test_generic_homeassistant_rejects_the_old_keys() -> None:
    with pytest.raises(DeviceConfigError, match=r"oid_power moved to 'capabilities: \{power_w"):
        plugin_registry.build_device(
            "c", {"type": "generic_homeassistant", "category": "consumer",
                  "oid_power": "sensor.x", "capabilities": {}},
            BuildContext(backends=BackendsConfig(), ha_client=_FakeHA()),
        )


def test_generic_homeassistant_import_export_binding() -> None:
    dev = plugin_registry.build_device(
        "g", {"type": "generic_homeassistant", "category": "grid_meter",
              "capabilities": {"power_w": {"import_entity": "sensor.i",
                                           "export_entity": "sensor.e"}}},
        BuildContext(backends=BackendsConfig(), ha_client=_FakeHA()),
    )
    assert (dev._entity_power_import, dev._entity_power_export) == ("sensor.i", "sensor.e")


# ── composition ──────────────────────────────────────────────────────────────


def test_storage_constraints_from_battery_params() -> None:
    specs = parse_devices({
        "grid": GRID,
        "bat": {**BATTERY, "metered_by": "grid", "params": {
            **BATTERY["params"], "min_soc_pct": 5, "charge_efficiency": 0.9,
            "discharge_efficiency": 0.9, "purchase_price_eur": 2700, "cycle_life": 6000,
            "no_grid_charge": True, "standby_w": 20,
        }},
    })
    (sc,) = storage_constraints(specs)
    assert sc.device_id == "bat"
    assert (sc.capacity_kwh, sc.max_charge_kw, sc.max_discharge_kw) == (7.6, 1.2, 0.8)
    assert (sc.min_soc_pct, sc.max_soc_pct) == (5.0, 100.0)
    assert (sc.charge_efficiency, sc.discharge_efficiency) == (0.9, 0.9)
    assert (sc.purchase_price_eur, sc.cycle_life) == (2700.0, 6000)
    assert sc.no_grid_charge is True and sc.standby_w == 20.0

    composed = compose(specs)
    assert composed.storage_constraints == [sc]
    assert composed.topology is not None
    assert composed.topology.all_device_ids() == ["grid", "bat"]


def test_storage_constraint_defaults() -> None:
    (sc,) = storage_constraints(parse_devices({"bat": BATTERY}))
    assert (sc.charge_efficiency, sc.discharge_efficiency) == (0.95, 0.95)
    assert (sc.min_soc_pct, sc.max_soc_pct, sc.standby_w) == (0.0, 100.0, 50.0)
    assert sc.purchase_price_eur is None and sc.cycle_life is None
    assert sc.no_grid_charge is False

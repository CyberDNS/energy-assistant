"""Device presets: every one is a valid device with its example values."""

from __future__ import annotations

import json
import re

import pytest
from helpers.fake_ha_client import FakeHAClient

from nornwatt.core.config import BackendsConfig
from nornwatt.core.device_model import parse_devices
from nornwatt.core.plugin_registry import BuildContext
from nornwatt.core.presets import apply_preset, load_presets
from nornwatt.loader.device_loader import validate_plugins
from nornwatt.plugins import registry

PRESETS = load_presets()


def test_presets_are_found_and_complete() -> None:
    assert len(PRESETS) >= 6
    for p in PRESETS:
        assert {"id", "title", "category", "integration", "description", "device"} <= set(p), p["id"]
        assert isinstance(p.get("verified"), bool), p["id"]
        for name, spec in (p.get("variables") or {}).items():
            assert spec.get("label") and spec.get("example"), f"{p['id']}: {name}"
    assert {p["category"] for p in PRESETS} >= {"battery", "charger", "thermal_load"}


@pytest.mark.parametrize("preset", PRESETS, ids=[p["id"] for p in PRESETS])
def test_preset_is_a_valid_device(preset) -> None:
    entry = apply_preset(preset)
    assert not re.search(r"\{[a-z_]+\}", json.dumps(entry)), "a variable was not filled in"
    specs = parse_devices({"dev": entry})
    validate_plugins(specs)
    device = registry.build_device(
        "dev", specs["dev"].plugin_config(),
        BuildContext(backends=BackendsConfig(), ha_client=FakeHAClient()))
    assert device is not None


def test_variables_are_filled_in() -> None:
    preset = next(p for p in PRESETS if p["id"] == "openwb_chargepoint")
    entry = apply_preset(preset, {"prefix": "openwb_cp_1"})
    assert entry["capabilities"]["power_setpoint"]["mode"] == "select.openwb_cp_1_lademodus"
    assert entry["phases"] == 3            # settings are merged into the entry


def test_schema_carries_labels_and_key_choices() -> None:
    spec = registry.device_info("homeassistant_battery").to_dict()["bindings"]["power_setpoint"]
    assert spec["control"]["choice_labels"]["mode_power"] == "Working mode + power"
    assert spec["modes"]["keys"]["choices"][:2] == ["charge", "discharge"]
    assert spec["modes"]["keys"]["choice_labels"]["absorb_surplus"] == "Store PV surplus only"


def test_unknown_mode_key_is_a_config_error() -> None:
    from nornwatt.core.device_model import DeviceConfigError

    entry = apply_preset(next(p for p in PRESETS if p["id"] == "huawei_luna2000"))
    entry["capabilities"]["power_setpoint"]["modes"]["turbo"] = {"option": "x"}
    with pytest.raises(DeviceConfigError, match="key 'turbo' must be one of"):
        validate_plugins(parse_devices({"dev": entry}))

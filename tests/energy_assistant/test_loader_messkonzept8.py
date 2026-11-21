"""Tests for YamlConfigLoader and device_loader.build() with Messkonzept 8."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from energy_assistant.config.yaml import YamlConfigLoader
from energy_assistant.core.config import AppConfig
from energy_assistant.core.device_model import (
    DeviceCategory,
    DeviceConfigError,
    parse_devices,
)
from energy_assistant.core.topology import TopologyNode
from energy_assistant.loader.device_loader import build
from energy_assistant.plugins.differential.device import DifferentialDevice
from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
from energy_assistant.plugins.generic_iobroker.device import GenericIoBrokerDevice

# ---------------------------------------------------------------------------
# YamlConfigLoader tests
# ---------------------------------------------------------------------------


class TestYamlConfigLoader:
    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        loader = YamlConfigLoader(tmp_path / "nonexistent.yaml")
        cfg = loader.load()
        assert isinstance(cfg, AppConfig)
        assert cfg.devices == {}
        assert cfg.tariffs == {}

    def test_parses_backends(self, tmp_path: Path) -> None:
        yaml_text = dedent("""\
            backends:
              iobroker:
                host: "192.168.1.10"
                port: 8087
        """)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_text)

        cfg = YamlConfigLoader(config_file).load()
        assert cfg.backends.iobroker is not None
        assert cfg.backends.iobroker.host == "192.168.1.10"
        assert cfg.backends.iobroker.port == 8087

    def test_parses_tariffs(self, tmp_path: Path) -> None:
        yaml_text = dedent("""\
            tariffs:
              export_tariff:
                type: flat_rate
                import_price_eur_per_kwh: 0.082
        """)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_text)

        cfg = YamlConfigLoader(config_file).load()
        assert "export_tariff" in cfg.tariffs
        assert cfg.tariffs["export_tariff"]["import_price_eur_per_kwh"] == pytest.approx(0.082)

    def test_parses_devices(self, tmp_path: Path) -> None:
        yaml_text = dedent("""\
            devices:
              main_grid_meter:
                category: grid_meter
                type: generic_iobroker
                power_import: "tibberlink.0.Homes.abc.powerImport"
                power_export: "tibberlink.0.Homes.abc.powerExport"
        """)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_text)

        cfg = YamlConfigLoader(config_file).load()
        spec = cfg.devices["main_grid_meter"]
        assert spec.category is DeviceCategory.GRID_METER
        assert spec.type == "generic_iobroker"
        assert spec.settings["power_import"] == "tibberlink.0.Homes.abc.powerImport"

    def test_topology_section_is_rejected(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("topology:\n  - main_grid_meter: {}\n")
        with pytest.raises(DeviceConfigError, match="metered_by"):
            YamlConfigLoader(config_file).load()

    def test_role_is_rejected(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("devices:\n  - id: m\n    role: meter\n    type: tasmota\n")
        with pytest.raises(DeviceConfigError, match="'role:' was replaced by 'category:'"):
            YamlConfigLoader(config_file).load()

    def test_secret_tag_resolves_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENERGY_ASSISTANT_SECRET_MYTOKEN", "secret-value-123")
        yaml_text = dedent("""\
            tariffs:
              tibber:
                type: tibber_iobroker
                home_id: !secret mytoken
        """)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_text)

        cfg = YamlConfigLoader(config_file).load()
        assert cfg.tariffs["tibber"]["home_id"] == "secret-value-123"


# ---------------------------------------------------------------------------
# device_loader.build() tests — Messkonzept 8 integration
# ---------------------------------------------------------------------------


def _messkonzept8_config(tmp_path: Path) -> AppConfig:
    """Build an AppConfig representing the Messkonzept 8 setup."""
    yaml_text = dedent("""\
        backends:
          iobroker:
            host: "192.168.1.5"
            port: 8087

        tariffs:
          export_tariff:
            type: flat_rate
            import_price_eur_per_kwh: 0.082
          household_tariff:
            type: flat_rate
            import_price_eur_per_kwh: 0.30
          heatpump_tariff:
            type: flat_rate
            import_price_eur_per_kwh: 0.15

        devices:
          main_grid_meter:
            category: grid_meter
            type: generic_iobroker
            power_import: "tibberlink.0.Homes.abc.powerImport"
            power_export: "tibberlink.0.Homes.abc.powerExport"
            tariff: export_tariff

          household_meter:
            category: meter
            type: generic_iobroker
            metered_by: main_grid_meter
            power: "sma-em.0.12345.wirkleistung_bezug"
            tariff: household_tariff

          heatpump:
            category: consumer
            type: differential
            metered_by: main_grid_meter
            minuend: main_grid_meter
            subtrahend: household_meter
            min_w: 0.0
            tariff: heatpump_tariff
    """)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_text)
    return YamlConfigLoader(config_file).load()


class TestDeviceLoaderMesskonzept8:
    def test_all_three_devices_registered(self, tmp_path: Path) -> None:
        cfg = _messkonzept8_config(tmp_path)
        registry, tariffs, topology = build(cfg)

        assert len(registry) == 3
        assert registry.get("main_grid_meter") is not None
        assert registry.get("household_meter") is not None
        assert registry.get("heatpump") is not None

    def test_device_categories(self, tmp_path: Path) -> None:
        cfg = _messkonzept8_config(tmp_path)
        registry, _, _ = build(cfg)

        assert registry.get("main_grid_meter").category == DeviceCategory.GRID_METER
        assert registry.get("household_meter").category == DeviceCategory.METER
        assert registry.get("heatpump").category == DeviceCategory.CONSUMER

    def test_device_types(self, tmp_path: Path) -> None:
        cfg = _messkonzept8_config(tmp_path)
        registry, _, _ = build(cfg)

        assert isinstance(registry.get("main_grid_meter"), GenericIoBrokerDevice)
        assert isinstance(registry.get("household_meter"), GenericIoBrokerDevice)
        assert isinstance(registry.get("heatpump"), DifferentialDevice)

    def test_all_three_tariffs_built(self, tmp_path: Path) -> None:
        cfg = _messkonzept8_config(tmp_path)
        _, tariffs, _ = build(cfg)

        assert "export_tariff" in tariffs
        assert "household_tariff" in tariffs
        assert "heatpump_tariff" in tariffs
        assert isinstance(tariffs["export_tariff"], FlatRateTariff)

    def test_topology_root_is_main_grid_meter(self, tmp_path: Path) -> None:
        cfg = _messkonzept8_config(tmp_path)
        _, _, topology = build(cfg)

        assert topology is not None
        assert isinstance(topology, TopologyNode)
        assert topology.device_id == "main_grid_meter"

    def test_topology_children(self, tmp_path: Path) -> None:
        cfg = _messkonzept8_config(tmp_path)
        _, _, topology = build(cfg)

        assert topology is not None
        child_ids = {c.device_id for c in topology.children}
        assert child_ids == {"household_meter", "heatpump"}

    def test_by_category_queries(self, tmp_path: Path) -> None:
        cfg = _messkonzept8_config(tmp_path)
        registry, _, _ = build(cfg)

        meters = registry.by_category(DeviceCategory.GRID_METER, DeviceCategory.METER)
        consumers = registry.by_category(DeviceCategory.CONSUMER)

        assert [m.device_id for m in meters] == ["main_grid_meter", "household_meter"]
        assert len(consumers) == 1
        assert consumers[0].device_id == "heatpump"

    def test_missing_backend_skips_iobroker_devices(self, tmp_path: Path) -> None:
        """When no ioBroker backend is configured, generic_iobroker devices are skipped."""
        from energy_assistant.core.config import AppConfig

        cfg = AppConfig(
            devices=parse_devices({
                "main_grid_meter": {
                    "category": "meter",
                    "type": "generic_iobroker",
                    "power": "some.oid",
                }
            })
        )
        registry, _, _ = build(cfg)
        # Device skipped because no ioBroker backend configured
        assert registry.get("main_grid_meter") is None


# ---------------------------------------------------------------------------
# The shipped example configs load and validate
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["config.yaml.example", "config_messkonzept8.yaml.example"])
def test_example_config_is_valid(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from energy_assistant.core.topology import build_topology
    from energy_assistant.loader.device_loader import validate_plugins

    for secret in ("TIBBER_HOME_ID", "TIBBER_PULSE_PASSWORD", "ZENDURE_CLOUD_KEY"):
        monkeypatch.setenv(f"ENERGY_ASSISTANT_SECRET_{secret}", "x")
    cfg = YamlConfigLoader(_REPO / name).load()
    validate_plugins(cfg.devices)
    root = build_topology(cfg.devices)
    assert root is not None and root.device_id == "main_grid_meter"
    assert root.find("household_meter") is not None

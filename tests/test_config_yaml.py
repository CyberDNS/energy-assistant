"""Tests for YamlConfigManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from energy_manager.config.yaml import YamlConfigManager


async def test_load_entries_from_valid_yaml(tmp_yaml: Path) -> None:
    tmp_yaml.write_text(
        """
devices:
  - id: solar_inverter
    plugin: energy_manager.plugins.iobroker
    data:
      host: 192.168.1.5
      port: 8087
  - id: heat_pump
    plugin: energy_manager.plugins.iobroker
    tariff_id: waermepumpe
    data:
      host: 192.168.1.5
      port: 8087
"""
    )
    manager = YamlConfigManager(tmp_yaml)
    entries = await manager.load_entries()

    assert len(entries) == 2
    assert entries[0].id == "solar_inverter"
    assert entries[0].plugin == "energy_manager.plugins.iobroker"
    assert entries[0].data["host"] == "192.168.1.5"
    assert entries[0].tariff_id is None
    assert entries[1].id == "heat_pump"
    assert entries[1].tariff_id == "waermepumpe"


async def test_load_entries_nonexistent_file_returns_empty(tmp_path: Path) -> None:
    manager = YamlConfigManager(tmp_path / "missing.yaml")
    entries = await manager.load_entries()
    assert entries == []


async def test_load_entries_empty_file_returns_empty(tmp_yaml: Path) -> None:
    tmp_yaml.write_text("")
    manager = YamlConfigManager(tmp_yaml)
    assert await manager.load_entries() == []


async def test_load_entries_no_devices_key_returns_empty(tmp_yaml: Path) -> None:
    tmp_yaml.write_text("integrations: []")
    manager = YamlConfigManager(tmp_yaml)
    assert await manager.load_entries() == []


async def test_load_entry_with_no_data_field(tmp_yaml: Path) -> None:
    tmp_yaml.write_text(
        """
devices:
  - id: meter
    plugin: energy_manager.plugins.iobroker
"""
    )
    manager = YamlConfigManager(tmp_yaml)
    entries = await manager.load_entries()
    assert entries[0].data == {}


async def test_save_entry_raises(tmp_yaml: Path) -> None:
    from energy_manager.core.models import ConfigEntry

    manager = YamlConfigManager(tmp_yaml)
    entry = ConfigEntry(id="x", plugin="some.plugin")
    with pytest.raises(NotImplementedError):
        await manager.save_entry(entry)


async def test_delete_entry_raises(tmp_yaml: Path) -> None:
    manager = YamlConfigManager(tmp_yaml)
    with pytest.raises(NotImplementedError):
        await manager.delete_entry("x")

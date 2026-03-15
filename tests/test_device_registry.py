"""Tests for DeviceRegistry."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from energy_manager.core.device import Device
from energy_manager.core.models import DeviceCategory, DeviceCommand, DeviceState
from energy_manager.core.registry import DeviceRegistry


# ---------------------------------------------------------------------------
# Fake device — implements Device structurally (no subclassing)
# ---------------------------------------------------------------------------


class FakeDevice:
    def __init__(self, device_id: str, category: DeviceCategory) -> None:
        self._device_id = device_id
        self._category = category

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return self._category

    async def get_state(self) -> DeviceState:
        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
        )

    async def send_command(self, command: DeviceCommand) -> None:
        pass


def _device(device_id: str, category: DeviceCategory = DeviceCategory.SOURCE) -> FakeDevice:
    return FakeDevice(device_id, category)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fake_device_satisfies_protocol() -> None:
    assert isinstance(_device("x"), Device)


def test_register_and_get() -> None:
    registry = DeviceRegistry()
    device = _device("solar")
    registry.register(device)
    assert registry.get("solar") is device


def test_get_unknown_returns_none() -> None:
    registry = DeviceRegistry()
    assert registry.get("nonexistent") is None


def test_register_replaces_existing_entry() -> None:
    registry = DeviceRegistry()
    old = _device("solar")
    new = _device("solar")
    registry.register(old)
    registry.register(new)
    assert registry.get("solar") is new


def test_unregister_removes_device() -> None:
    registry = DeviceRegistry()
    registry.register(_device("solar"))
    registry.unregister("solar")
    assert registry.get("solar") is None


def test_unregister_nonexistent_is_noop() -> None:
    registry = DeviceRegistry()
    registry.unregister("ghost")  # must not raise


def test_all_returns_all_devices() -> None:
    registry = DeviceRegistry()
    registry.register(_device("d1"))
    registry.register(_device("d2"))
    registry.register(_device("d3"))
    ids = {d.device_id for d in registry.all()}
    assert ids == {"d1", "d2", "d3"}


def test_all_empty_registry() -> None:
    assert DeviceRegistry().all() == []


def test_by_category_filters_correctly() -> None:
    registry = DeviceRegistry()
    registry.register(_device("solar", DeviceCategory.SOURCE))
    registry.register(_device("battery", DeviceCategory.STORAGE))
    registry.register(_device("ev", DeviceCategory.CONSUMER))
    registry.register(_device("grid", DeviceCategory.METER))

    sources = registry.by_category(DeviceCategory.SOURCE)
    assert len(sources) == 1
    assert sources[0].device_id == "solar"


def test_by_category_no_match_returns_empty() -> None:
    registry = DeviceRegistry()
    registry.register(_device("solar", DeviceCategory.SOURCE))
    assert registry.by_category(DeviceCategory.CONSUMER) == []


def test_by_category_multiple_devices_same_category() -> None:
    registry = DeviceRegistry()
    registry.register(_device("ev1", DeviceCategory.CONSUMER))
    registry.register(_device("ev2", DeviceCategory.CONSUMER))
    registry.register(_device("heat_pump", DeviceCategory.CONSUMER))

    consumers = registry.by_category(DeviceCategory.CONSUMER)
    assert {d.device_id for d in consumers} == {"ev1", "ev2", "heat_pump"}

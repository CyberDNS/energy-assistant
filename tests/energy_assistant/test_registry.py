"""Tests for DeviceRegistry."""

from __future__ import annotations

import pytest

from energy_assistant.core.models import DeviceCommand, DeviceRole, DeviceState
from energy_assistant.core.registry import DeviceRegistry


class _FakeDevice:
    def __init__(self, device_id: str, role: DeviceRole = DeviceRole.METER) -> None:
        self._device_id = device_id
        self._role = role

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

    async def get_state(self) -> DeviceState:
        return DeviceState(device_id=self._device_id)

    async def send_command(self, command: DeviceCommand) -> None:
        pass


class TestDeviceRegistry:
    def test_register_and_get(self) -> None:
        registry = DeviceRegistry()
        device = _FakeDevice("meter1")
        registry.register(device)
        assert registry.get("meter1") is device

    def test_get_unknown_returns_none(self) -> None:
        registry = DeviceRegistry()
        assert registry.get("nonexistent") is None

    def test_register_overwrites_existing(self) -> None:
        registry = DeviceRegistry()
        d1 = _FakeDevice("meter1")
        d2 = _FakeDevice("meter1")
        registry.register(d1)
        registry.register(d2)
        assert registry.get("meter1") is d2

    def test_unregister(self) -> None:
        registry = DeviceRegistry()
        registry.register(_FakeDevice("meter1"))
        registry.unregister("meter1")
        assert registry.get("meter1") is None

    def test_unregister_nonexistent_is_safe(self) -> None:
        registry = DeviceRegistry()
        registry.unregister("does_not_exist")  # must not raise

    def test_all_returns_all_registered(self) -> None:
        registry = DeviceRegistry()
        d1 = _FakeDevice("m1", DeviceRole.METER)
        d2 = _FakeDevice("m2", DeviceRole.CONSUMER)
        d3 = _FakeDevice("m3", DeviceRole.PRODUCER)
        registry.register(d1)
        registry.register(d2)
        registry.register(d3)
        assert set(d.device_id for d in registry.all()) == {"m1", "m2", "m3"}

    def test_by_role_filters_correctly(self) -> None:
        registry = DeviceRegistry()
        registry.register(_FakeDevice("meter1", DeviceRole.METER))
        registry.register(_FakeDevice("meter2", DeviceRole.METER))
        registry.register(_FakeDevice("consumer1", DeviceRole.CONSUMER))

        meters = registry.by_role(DeviceRole.METER)
        assert len(meters) == 2
        assert all(d.role == DeviceRole.METER for d in meters)

        consumers = registry.by_role(DeviceRole.CONSUMER)
        assert len(consumers) == 1

    def test_len(self) -> None:
        registry = DeviceRegistry()
        assert len(registry) == 0
        registry.register(_FakeDevice("m1"))
        registry.register(_FakeDevice("m2"))
        assert len(registry) == 2

    def test_state_cache_roundtrip(self) -> None:
        registry = DeviceRegistry()
        registry.register(_FakeDevice("meter1"))

        assert registry.latest_state("meter1") is None

        state = DeviceState(device_id="meter1", power_w=1500.0)
        registry.update_state(state)

        cached = registry.latest_state("meter1")
        assert cached is not None
        assert cached.power_w == pytest.approx(1500.0)

    def test_state_cache_unregister_clears_state(self) -> None:
        registry = DeviceRegistry()
        registry.register(_FakeDevice("meter1"))
        registry.update_state(DeviceState(device_id="meter1", power_w=1000.0))
        registry.unregister("meter1")
        assert registry.latest_state("meter1") is None

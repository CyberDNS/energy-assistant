"""Tests for SMAModbusIoBrokerDevice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_iobroker_client import FakeIoBrokerClient

from energy_manager.core.models import DeviceCategory
from energy_manager.plugins.sma_modbus_iobroker.device import (
    SMAModbusIoBrokerDevice,
    _DEFAULT_OID_MAX_CHARGE_W,
    _DEFAULT_OID_MAX_DISCHARGE_W,
    _DEFAULT_OID_POWER_W,
    _DEFAULT_OID_SOC,
)

_PREFIX = "modbus.0.inputRegisters"


def _device(store: dict | None = None, **kwargs) -> SMAModbusIoBrokerDevice:
    return SMAModbusIoBrokerDevice(
        device_id="sma_battery",
        client=FakeIoBrokerClient(store or {}),
        **kwargs,
    )


def _store(
    soc: float = 60.0,
    power_w: float = 0.0,  # negative = charging, positive = discharging
    max_charge_w: float = 3680.0,
    max_discharge_w: float = 3900.0,
) -> dict:
    return {
        _DEFAULT_OID_SOC: soc,
        _DEFAULT_OID_POWER_W: power_w,
        _DEFAULT_OID_MAX_CHARGE_W: max_charge_w,
        _DEFAULT_OID_MAX_DISCHARGE_W: max_discharge_w,
    }


# ---------------------------------------------------------------------------
# Tests — identity & controllability
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_device_id(self):
        assert _device().device_id == "sma_battery"

    def test_category_is_storage(self):
        assert _device().category == DeviceCategory.STORAGE

    def test_storage_constraints_is_none(self):
        """SMA battery is not controllable — must return None."""
        assert _device().storage_constraints is None


# ---------------------------------------------------------------------------
# Tests — get_state
# ---------------------------------------------------------------------------


class TestGetState:
    async def test_soc_pct(self):
        state = await _device(_store(soc=75.0)).get_state()
        assert state.soc_pct == pytest.approx(75.0)

    async def test_power_charging_is_negative(self):
        """PowerAC negative = charging → power_w must be negative."""
        state = await _device(_store(power_w=-600.0)).get_state()
        assert state.power_w == pytest.approx(-600.0)

    async def test_power_discharging_is_positive(self):
        """PowerAC positive = discharging → power_w must be positive."""
        state = await _device(_store(power_w=400.0)).get_state()
        assert state.power_w == pytest.approx(400.0)

    async def test_power_idle_is_zero(self):
        state = await _device(_store(power_w=0.0)).get_state()
        assert state.power_w == pytest.approx(0.0)

    async def test_extra_contains_category(self):
        state = await _device(_store()).get_state()
        assert state.extra["category"] == DeviceCategory.STORAGE.value

    async def test_extra_marks_not_controllable(self):
        state = await _device(_store()).get_state()
        assert state.extra["controllable"] is False

    async def test_missing_oids_do_not_raise(self):
        """Store with no keys should return a valid (mostly-None) state."""
        state = await _device({}).get_state()
        assert state.device_id == "sma_battery"
        assert state.soc_pct is None
        assert state.power_w is None

    async def test_available_is_true(self):
        state = await _device(_store()).get_state()
        assert state.available is True


# ---------------------------------------------------------------------------
# Tests — custom OID configuration
# ---------------------------------------------------------------------------


class TestCustomOIDs:
    async def test_custom_oid_soc(self):
        """Custom OID names must be used for register reads."""
        store = {
            "modbus.0.inputRegisters.custom_power": -300.0,
            "modbus.0.inputRegisters.custom_soc": 88.0,
        }
        device = _device(
            store,
            oid_power_w="modbus.0.inputRegisters.custom_power",
            oid_soc="modbus.0.inputRegisters.custom_soc",
        )
        state = await device.get_state()
        assert state.soc_pct == pytest.approx(88.0)
        assert state.power_w == pytest.approx(-300.0)

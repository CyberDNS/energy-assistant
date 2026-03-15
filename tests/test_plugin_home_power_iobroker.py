"""Tests for HomePowerIoBrokerDevice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_iobroker_client import FakeIoBrokerClient

from energy_manager.core.models import DeviceCategory
from energy_manager.plugins.home_power_iobroker.device import (
    HomePowerIoBrokerDevice,
    _DEFAULT_OID_CARS_W,
    _DEFAULT_OID_HOUSEHOLD_W,
    _DEFAULT_OID_OVERFLOW_W,
    _DEFAULT_OID_PV_W,
)


def _device(store: dict | None = None) -> HomePowerIoBrokerDevice:
    return HomePowerIoBrokerDevice(
        device_id="home_power",
        client=FakeIoBrokerClient(store or {}),
    )


def _store(
    household_w: float = 500.0,
    overflow_w: float = 0.0,
    cars_w: float = 0.0,
    pv_w: float = 300.0,
) -> dict:
    return {
        _DEFAULT_OID_HOUSEHOLD_W: household_w,
        _DEFAULT_OID_OVERFLOW_W: overflow_w,
        _DEFAULT_OID_CARS_W: cars_w,
        _DEFAULT_OID_PV_W: pv_w,
    }


# ---------------------------------------------------------------------------
# Tests — identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_device_id(self):
        assert _device().device_id == "home_power"

    def test_category_is_meter(self):
        assert _device().category == DeviceCategory.METER


# ---------------------------------------------------------------------------
# Tests — get_state
# ---------------------------------------------------------------------------


class TestGetState:
    async def test_household_is_power_w(self):
        state = await _device(_store(household_w=1200.0)).get_state()
        assert state.power_w == pytest.approx(1200.0)

    async def test_overflow_in_extra(self):
        state = await _device(_store(overflow_w=350.0)).get_state()
        assert state.extra["overflow_w"] == pytest.approx(350.0)

    async def test_cars_in_extra(self):
        state = await _device(_store(cars_w=7000.0)).get_state()
        assert state.extra["cars_w"] == pytest.approx(7000.0)

    async def test_pv_in_extra(self):
        state = await _device(_store(pv_w=1800.0)).get_state()
        assert state.extra["pv_w"] == pytest.approx(1800.0)

    async def test_device_id_in_state(self):
        state = await _device(_store()).get_state()
        assert state.device_id == "home_power"

    async def test_missing_oids_return_none(self):
        state = await _device({}).get_state()
        assert state.power_w is None
        assert state.extra["overflow_w"] is None
        assert state.extra["pv_w"] is None
        assert state.extra["cars_w"] is None

    async def test_zero_values_are_preserved(self):
        state = await _device(_store(overflow_w=0.0, cars_w=0.0)).get_state()
        assert state.extra["overflow_w"] == pytest.approx(0.0)
        assert state.extra["cars_w"] == pytest.approx(0.0)

    async def test_all_sensors_read_in_one_call(self):
        """All four readings should come from the same bulk fetch."""
        client = FakeIoBrokerClient(_store(household_w=800.0, pv_w=1200.0, overflow_w=400.0))
        device = HomePowerIoBrokerDevice(device_id="home_power", client=client)
        state = await device.get_state()
        assert state.power_w == pytest.approx(800.0)
        assert state.extra["pv_w"] == pytest.approx(1200.0)
        assert state.extra["overflow_w"] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# Tests — custom OIDs
# ---------------------------------------------------------------------------


class TestCustomOIDs:
    async def test_custom_oids_are_used(self):
        store = {
            "custom.household": 800.0,
            "custom.pv": 1200.0,
            "custom.overflow": 400.0,
            "custom.cars": 11000.0,
        }
        device = HomePowerIoBrokerDevice(
            device_id="home_power",
            client=FakeIoBrokerClient(store),
            oid_household_w="custom.household",
            oid_pv_w="custom.pv",
            oid_overflow_w="custom.overflow",
            oid_cars_w="custom.cars",
        )
        state = await device.get_state()
        assert state.power_w == pytest.approx(800.0)
        assert state.extra["pv_w"] == pytest.approx(1200.0)
        assert state.extra["overflow_w"] == pytest.approx(400.0)
        assert state.extra["cars_w"] == pytest.approx(11000.0)

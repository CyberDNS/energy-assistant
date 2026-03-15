"""Tests for SMAEMIoBrokerDevice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_iobroker_client import FakeIoBrokerClient

from energy_manager.core.models import DeviceCategory
from energy_manager.plugins.sma_em_iobroker.device import SMAEMIoBrokerDevice

_SERIAL = "3008815327"
_OID_IMPORT = f"sma-em.0.{_SERIAL}.pregard"
_OID_EXPORT = f"sma-em.0.{_SERIAL}.psurplus"


def _device(store: dict | None = None, **kwargs) -> SMAEMIoBrokerDevice:
    return SMAEMIoBrokerDevice(
        device_id="grid_meter",
        client=FakeIoBrokerClient(store or {}),
        serial=_SERIAL,
        **kwargs,
    )


def _store(import_w: float = 0.0, export_w: float = 0.0) -> dict:
    return {
        _OID_IMPORT: import_w,
        _OID_EXPORT: export_w,
    }


# ---------------------------------------------------------------------------
# Tests — identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_device_id(self):
        assert _device().device_id == "grid_meter"

    def test_category_is_meter(self):
        assert _device().category == DeviceCategory.METER

    def test_default_oid_import(self):
        d = _device()
        assert d._oid_import_w == _OID_IMPORT

    def test_default_oid_export(self):
        d = _device()
        assert d._oid_export_w == _OID_EXPORT

    def test_oid_override(self):
        d = _device(oid_import_w="custom.pregard", oid_export_w="custom.psurplus")
        assert d._oid_import_w == "custom.pregard"
        assert d._oid_export_w == "custom.psurplus"


# ---------------------------------------------------------------------------
# Tests — get_state
# ---------------------------------------------------------------------------


class TestGetState:
    async def test_net_import(self):
        """Importing from grid: power_w should be positive."""
        state = await _device(_store(import_w=1500.0, export_w=0.0)).get_state()
        assert state.power_w == pytest.approx(1500.0)

    async def test_net_export(self):
        """Exporting to grid: power_w should be negative."""
        state = await _device(_store(import_w=0.0, export_w=800.0)).get_state()
        assert state.power_w == pytest.approx(-800.0)

    async def test_net_mixed(self):
        """Both channels active: net = import - export."""
        state = await _device(_store(import_w=300.0, export_w=100.0)).get_state()
        assert state.power_w == pytest.approx(200.0)

    async def test_extra_import_w(self):
        state = await _device(_store(import_w=1200.0, export_w=0.0)).get_state()
        assert state.extra["import_w"] == pytest.approx(1200.0)

    async def test_extra_export_w(self):
        state = await _device(_store(import_w=0.0, export_w=500.0)).get_state()
        assert state.extra["export_w"] == pytest.approx(500.0)

    async def test_device_id_in_state(self):
        state = await _device(_store()).get_state()
        assert state.device_id == "grid_meter"

    async def test_both_none_gives_none_power_w(self):
        """If adapter returns nothing for both OIDs, power_w is None."""
        state = await _device({}).get_state()
        assert state.power_w is None
        assert state.extra["import_w"] is None
        assert state.extra["export_w"] is None

    async def test_only_import_oid_present(self):
        """If only pregard is available, power_w equals import_w."""
        store = {_OID_IMPORT: 750.0}
        state = await _device(store).get_state()
        assert state.power_w == pytest.approx(750.0)
        assert state.extra["export_w"] is None

    async def test_only_export_oid_present(self):
        """If only psurplus is available, power_w equals -export_w."""
        store = {_OID_EXPORT: 400.0}
        state = await _device(store).get_state()
        assert state.power_w == pytest.approx(-400.0)
        assert state.extra["import_w"] is None

    async def test_non_numeric_value_returns_none(self):
        store = {_OID_IMPORT: "n/a", _OID_EXPORT: 0.0}
        state = await _device(store).get_state()
        assert state.extra["import_w"] is None

    async def test_zero_net_at_balance(self):
        """Exact balance: import == export → net = 0."""
        state = await _device(_store(import_w=500.0, export_w=500.0)).get_state()
        assert state.power_w == pytest.approx(0.0)

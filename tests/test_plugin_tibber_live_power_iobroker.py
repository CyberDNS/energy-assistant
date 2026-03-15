"""Tests for TibberLivePowerIoBrokerDevice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_iobroker_client import FakeIoBrokerClient

from energy_manager.core.models import DeviceCategory
from energy_manager.plugins.tibber_iobroker.live_power import TibberLivePowerIoBrokerDevice

_HOME_ID = "aa115263-6d29-4e80-8190-fb95ddd4e743"
_OID_POWER = f"tibberlink.0.Homes.{_HOME_ID}.LiveMeasurement.power"


def _device(power_w: float | str | None = None, **kwargs) -> TibberLivePowerIoBrokerDevice:
    store = {} if power_w is None else {_OID_POWER: power_w}
    return TibberLivePowerIoBrokerDevice(
        device_id="tibber_live",
        client=FakeIoBrokerClient(store),
        home_id=_HOME_ID,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Tests — identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_device_id(self):
        assert _device().device_id == "tibber_live"

    def test_category_is_meter(self):
        assert _device().category == DeviceCategory.METER

    def test_default_oid(self):
        d = _device()
        assert d._oid_power_w == _OID_POWER

    def test_oid_override(self):
        custom = "tibberlink.0.Homes.other.LiveMeasurement.power"
        d = _device(oid_power_w=custom)
        assert d._oid_power_w == custom


# ---------------------------------------------------------------------------
# Tests — get_state
# ---------------------------------------------------------------------------


class TestGetState:
    async def test_importing_from_grid(self):
        """Positive value means importing from the grid."""
        state = await _device(power_w=1234.0).get_state()
        assert state.power_w == pytest.approx(1234.0)

    async def test_exporting_to_grid(self):
        """Negative value means exporting to the grid."""
        state = await _device(power_w=-800.0).get_state()
        assert state.power_w == pytest.approx(-800.0)

    async def test_zero_net(self):
        """Zero means balanced grid exchange."""
        state = await _device(power_w=0.0).get_state()
        assert state.power_w == pytest.approx(0.0)

    async def test_none_when_oid_missing(self):
        """OID not present in store → power_w is None."""
        state = await _device(power_w=None).get_state()
        assert state.power_w is None

    async def test_none_on_non_numeric_value(self):
        """Non-numeric string in store → power_w is None."""
        state = await _device(power_w="unavailable").get_state()
        assert state.power_w is None

    async def test_no_extra_fields(self):
        """Tibber device exposes only power_w, no extra fields."""
        state = await _device(power_w=500.0).get_state()
        assert state.extra == {}

    async def test_oid_override_is_read(self):
        """When oid_power_w is overridden, reads from the custom OID."""
        custom_oid = "tibberlink.0.Homes.other.LiveMeasurement.power"
        store = {custom_oid: 999.0}
        d = TibberLivePowerIoBrokerDevice(
            device_id="tibber_live",
            client=FakeIoBrokerClient(store),
            home_id=_HOME_ID,
            oid_power_w=custom_oid,
        )
        state = await d.get_state()
        assert state.power_w == pytest.approx(999.0)

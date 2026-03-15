"""Tests for MT175HADevice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_ha_client import FakeHAClient

from energy_manager.core.models import DeviceCategory
from energy_manager.plugins.mt175_ha.device import MT175HADevice, _DEFAULT_ENTITY_ID


def _device(power_w: float | str | None = None, **kwargs) -> MT175HADevice:
    states = {} if power_w is None else {_DEFAULT_ENTITY_ID: power_w}
    return MT175HADevice(device_id="mt175", client=FakeHAClient(states), **kwargs)


# ---------------------------------------------------------------------------
# Tests — identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_device_id(self):
        assert _device().device_id == "mt175"

    def test_category_is_meter(self):
        assert _device().category == DeviceCategory.METER

    def test_default_entity_id(self):
        assert _device()._entity_id == _DEFAULT_ENTITY_ID

    def test_entity_id_override(self):
        d = _device(entity_id="sensor.custom_meter")
        assert d._entity_id == "sensor.custom_meter"


# ---------------------------------------------------------------------------
# Tests — get_state
# ---------------------------------------------------------------------------


class TestGetState:
    async def test_importing_from_grid(self):
        """Positive value means consuming from grid."""
        state = await _device(power_w=1500.0).get_state()
        assert state.power_w == pytest.approx(1500.0)

    async def test_exporting_to_grid(self):
        """Negative value means feeding into grid."""
        state = await _device(power_w=-800.0).get_state()
        assert state.power_w == pytest.approx(-800.0)

    async def test_zero(self):
        state = await _device(power_w=0.0).get_state()
        assert state.power_w == pytest.approx(0.0)

    async def test_none_when_entity_missing(self):
        """Entity not in store → power_w is None."""
        state = await _device(power_w=None).get_state()
        assert state.power_w is None

    async def test_none_on_non_numeric(self):
        """HA returns 'unavailable' → power_w is None."""
        state = await _device(power_w="unavailable").get_state()
        assert state.power_w is None

    async def test_string_numeric_value(self):
        """HA state values are strings; they should parse correctly."""
        state = await _device(power_w="342.7").get_state()
        assert state.power_w == pytest.approx(342.7)

    async def test_no_extra_fields(self):
        state = await _device(power_w=100.0).get_state()
        assert state.extra == {}

    async def test_entity_id_override_is_read(self):
        custom = "sensor.custom_meter"
        client = FakeHAClient({custom: 999.0})
        d = MT175HADevice(device_id="mt175", client=client, entity_id=custom)
        state = await d.get_state()
        assert state.power_w == pytest.approx(999.0)

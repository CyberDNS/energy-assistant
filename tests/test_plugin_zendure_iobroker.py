"""Tests for ZendureIoBrokerDevice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_iobroker_client import FakeIoBrokerClient

from energy_manager.core.models import DeviceCategory, DeviceCommand
from energy_manager.plugins.zendure_iobroker.device import ZendureIoBrokerDevice

_HUB = "gDa3tb"
_SERIAL = "B1613x21"
_PREFIX = f"zendure-solarflow.0.{_HUB}.{_SERIAL}"


def _device(store: dict | None = None) -> ZendureIoBrokerDevice:
    return ZendureIoBrokerDevice(
        device_id="zendure",
        client=FakeIoBrokerClient(store or {}),
        hub_id=_HUB,
        device_serial=_SERIAL,
    )


def _full_store(
    soc: float = 50.0,
    charge_w: float = 0.0,
    discharge_w: float = 0.0,
    solar_w: float = 800.0,
    home_w: float = 400.0,
    min_soc: float = 10.0,
    max_soc: float = 100.0,
) -> dict:
    return {
        f"{_PREFIX}.electricLevel": soc,
        f"{_PREFIX}.outputPackPower": charge_w,
        f"{_PREFIX}.packInputPower": discharge_w,
        f"{_PREFIX}.solarInputPower": solar_w,
        f"{_PREFIX}.outputHomePower": home_w,
        f"{_PREFIX}.minSoc": min_soc,
        f"{_PREFIX}.socSet": max_soc,
    }


# ---------------------------------------------------------------------------
# Tests — identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_device_id(self):
        assert _device().device_id == "zendure"

    def test_category_is_storage(self):
        assert _device().category == DeviceCategory.STORAGE


# ---------------------------------------------------------------------------
# Tests — get_state
# ---------------------------------------------------------------------------


class TestGetState:
    async def test_soc_pct(self):
        state = await _device(_full_store(soc=42.0)).get_state()
        assert state.soc_pct == pytest.approx(42.0)

    async def test_power_charging_is_negative(self):
        """When battery is charging, power_w must be negative."""
        state = await _device(_full_store(charge_w=700.0, discharge_w=0.0)).get_state()
        assert state.power_w == pytest.approx(-700.0)

    async def test_power_discharging_is_positive(self):
        """When battery is discharging, power_w must be positive."""
        state = await _device(_full_store(charge_w=0.0, discharge_w=500.0)).get_state()
        assert state.power_w == pytest.approx(500.0)

    async def test_power_idle_is_zero(self):
        state = await _device(_full_store(charge_w=0.0, discharge_w=0.0)).get_state()
        assert state.power_w == pytest.approx(0.0)

    async def test_extra_contains_solar_input(self):
        state = await _device(_full_store(solar_w=1200.0)).get_state()
        assert state.extra["solar_input_w"] == pytest.approx(1200.0)

    async def test_extra_contains_category(self):
        state = await _device(_full_store()).get_state()
        assert state.extra["category"] == DeviceCategory.STORAGE.value

    async def test_extra_contains_soc_limits(self):
        state = await _device(_full_store(min_soc=15.0, max_soc=90.0)).get_state()
        assert state.extra["min_soc_pct"] == pytest.approx(15.0)
        assert state.extra["max_soc_pct"] == pytest.approx(90.0)

    async def test_missing_oids_do_not_raise(self):
        """Store with no keys at all should return a valid (mostly-None) state."""
        state = await _device({}).get_state()
        assert state.device_id == "zendure"
        assert state.soc_pct is None


# ---------------------------------------------------------------------------
# Tests — send_command
# ---------------------------------------------------------------------------


class TestSendCommand:
    async def test_set_automation_limit_writes_correct_oid(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.send_command(
            DeviceCommand(device_id="zendure", command="set_automation_limit", value=-800)
        )
        assert client.written == [(f"{_PREFIX}.control.setDeviceAutomationInOutLimit", -800)]

    async def test_set_charge_limit_writes_correct_oid(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.send_command(
            DeviceCommand(device_id="zendure", command="set_charge_limit", value=80)
        )
        assert client.written == [(f"{_PREFIX}.control.chargeLimit", 80)]

    async def test_set_discharge_limit_writes_correct_oid(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.send_command(
            DeviceCommand(device_id="zendure", command="set_discharge_limit", value=15)
        )
        assert client.written == [(f"{_PREFIX}.control.dischargeLimit", 15)]

    async def test_unknown_command_raises(self):
        device = _device()
        with pytest.raises(NotImplementedError):
            await device.send_command(
                DeviceCommand(device_id="zendure", command="fly_to_moon", value=None)
            )


# ---------------------------------------------------------------------------
# Tests — set_power_w
# ---------------------------------------------------------------------------


class TestSetPowerW:
    async def test_charge_sets_ac_mode_1_and_input_limit(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.set_power_w(-600.0)
        assert (f"{_PREFIX}.control.acMode", 1) in client.written
        assert (f"{_PREFIX}.control.setInputLimit", 600) in client.written
        assert (f"{_PREFIX}.control.setOutputLimit", 0) in client.written

    async def test_discharge_sets_ac_mode_2_and_output_limit(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.set_power_w(400.0)
        assert (f"{_PREFIX}.control.acMode", 2) in client.written
        assert (f"{_PREFIX}.control.setInputLimit", 0) in client.written
        assert (f"{_PREFIX}.control.setOutputLimit", 400) in client.written

    async def test_idle_clears_both_limits(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.set_power_w(0.0)
        assert (f"{_PREFIX}.control.setInputLimit", 0) in client.written
        assert (f"{_PREFIX}.control.setOutputLimit", 0) in client.written
        # acMode must NOT be written on idle (Zendure ignores limits when 0 anyway)
        assert all(oid != f"{_PREFIX}.control.acMode" for oid, _ in client.written)

    async def test_charge_truncates_to_int(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.set_power_w(-750.9)
        assert (f"{_PREFIX}.control.setInputLimit", 750) in client.written

    async def test_discharge_truncates_to_int(self):
        client = FakeIoBrokerClient()
        device = ZendureIoBrokerDevice("zendure", client, _HUB, _SERIAL)
        await device.set_power_w(333.7)
        assert (f"{_PREFIX}.control.setOutputLimit", 333) in client.written

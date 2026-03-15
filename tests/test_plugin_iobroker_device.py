"""Tests for IoBrokerDevice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_iobroker_client import FakeIoBrokerClient

from energy_manager.core.device import Device
from energy_manager.core.models import DeviceCategory, DeviceCommand
from energy_manager.plugins._iobroker.device import IoBrokerDevice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device(
    device_id: str = "solar",
    category: DeviceCategory = DeviceCategory.SOURCE,
    values: dict | None = None,
    state_map: dict | None = None,
    command_map: dict | None = None,
) -> tuple[IoBrokerDevice, FakeIoBrokerClient]:
    client = FakeIoBrokerClient(values or {})
    dev = IoBrokerDevice(
        device_id=device_id,
        category=category,
        client=client,
        state_map=state_map or {},
        command_map=command_map,
    )
    return dev, client


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_device_protocol() -> None:
    dev, _ = _device()
    assert isinstance(dev, Device)


def test_device_id_property() -> None:
    dev, _ = _device(device_id="ev_charger")
    assert dev.device_id == "ev_charger"


def test_category_property() -> None:
    dev, _ = _device(category=DeviceCategory.STORAGE)
    assert dev.category == DeviceCategory.STORAGE


# ---------------------------------------------------------------------------
# get_state — state mapping
# ---------------------------------------------------------------------------


async def test_get_state_maps_power_w() -> None:
    dev, _ = _device(
        values={"fronius.0.Power": 3500.0},
        state_map={"power_w": "fronius.0.Power"},
    )
    state = await dev.get_state()
    assert state.power_w == 3500.0


async def test_get_state_maps_all_known_fields() -> None:
    dev, _ = _device(
        category=DeviceCategory.STORAGE,
        values={
            "bat.power": -1200.0,
            "bat.energy": 8.5,
            "bat.soc": 72.0,
            "bat.available": True,
        },
        state_map={
            "power_w": "bat.power",
            "energy_kwh": "bat.energy",
            "soc_pct": "bat.soc",
            "available": "bat.available",
        },
    )
    state = await dev.get_state()
    assert state.power_w == -1200.0
    assert state.energy_kwh == 8.5
    assert state.soc_pct == 72.0
    assert state.available is True


async def test_get_state_unknown_field_goes_to_extra() -> None:
    dev, _ = _device(
        values={"inv.status": "running"},
        state_map={"status": "inv.status"},
    )
    state = await dev.get_state()
    assert state.extra["status"] == "running"


async def test_get_state_category_always_in_extra() -> None:
    dev, _ = _device(category=DeviceCategory.METER, state_map={})
    state = await dev.get_state()
    assert state.extra["category"] == "meter"


async def test_get_state_none_when_object_id_missing() -> None:
    dev, _ = _device(
        values={},
        state_map={"power_w": "nonexistent.object"},
    )
    state = await dev.get_state()
    assert state.power_w is None


async def test_get_state_empty_state_map_returns_defaults() -> None:
    dev, _ = _device(state_map={})
    state = await dev.get_state()
    assert state.power_w is None
    assert state.available is True
    assert state.device_id == "solar"


async def test_get_state_available_false_from_zero() -> None:
    dev, _ = _device(
        values={"dev.online": 0},
        state_map={"available": "dev.online"},
    )
    state = await dev.get_state()
    assert state.available is False


async def test_get_state_available_false_from_string() -> None:
    dev, _ = _device(
        values={"dev.online": "false"},
        state_map={"available": "dev.online"},
    )
    state = await dev.get_state()
    assert state.available is False


async def test_get_state_power_coerced_from_string() -> None:
    dev, _ = _device(
        values={"inv.power": "2400"},
        state_map={"power_w": "inv.power"},
    )
    state = await dev.get_state()
    assert state.power_w == 2400.0


async def test_get_state_power_none_on_non_numeric() -> None:
    dev, _ = _device(
        values={"inv.power": "n/a"},
        state_map={"power_w": "inv.power"},
    )
    state = await dev.get_state()
    assert state.power_w is None


# ---------------------------------------------------------------------------
# send_command
# ---------------------------------------------------------------------------


async def test_send_command_writes_correct_object_id() -> None:
    dev, client = _device(
        command_map={"set_current": "go-e.0.chargers.abc.amp"},
    )
    await dev.send_command(DeviceCommand(device_id="solar", command="set_current", value=16))
    assert client.written == [("go-e.0.chargers.abc.amp", 16)]


async def test_send_command_multiple_commands() -> None:
    dev, client = _device(
        command_map={
            "set_charge_power": "bat.chargeSetpoint",
            "set_discharge_power": "bat.dischargeSetpoint",
        },
    )
    await dev.send_command(DeviceCommand(device_id="bat", command="set_charge_power", value=2000))
    await dev.send_command(DeviceCommand(device_id="bat", command="set_discharge_power", value=1500))
    assert client.written[0] == ("bat.chargeSetpoint", 2000)
    assert client.written[1] == ("bat.dischargeSetpoint", 1500)


async def test_send_command_unknown_raises_not_implemented() -> None:
    dev, _ = _device(command_map={"set_current": "go-e.0.amp"})
    with pytest.raises(NotImplementedError, match="unknown_cmd"):
        await dev.send_command(DeviceCommand(device_id="x", command="unknown_cmd", value=0))


async def test_send_command_readonly_device_raises() -> None:
    dev, _ = _device(command_map=None)
    with pytest.raises(NotImplementedError, match="read-only"):
        await dev.send_command(DeviceCommand(device_id="x", command="set_power", value=0))


# ---------------------------------------------------------------------------
# All device categories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(DeviceCategory))
async def test_all_categories_supported(category: DeviceCategory) -> None:
    dev, _ = _device(category=category, state_map={"power_w": "x.Power"}, values={"x.Power": 100.0})
    state = await dev.get_state()
    assert state.extra["category"] == category.value

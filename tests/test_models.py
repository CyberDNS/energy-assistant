"""Tests for core data models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from energy_manager.core.models import (
    ConfigEntry,
    ControlAction,
    DeviceCategory,
    DeviceCommand,
    DeviceState,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    Measurement,
    TariffPoint,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestDeviceCategory:
    def test_values(self):
        assert DeviceCategory.SOURCE == "source"
        assert DeviceCategory.STORAGE == "storage"
        assert DeviceCategory.CONSUMER == "consumer"
        assert DeviceCategory.METER == "meter"


class TestDeviceState:
    def test_defaults(self):
        state = DeviceState(device_id="solar")
        assert state.device_id == "solar"
        assert state.power_w is None
        assert state.energy_kwh is None
        assert state.soc_pct is None
        assert state.available is True
        assert state.extra == {}
        assert isinstance(state.timestamp, datetime)

    def test_all_fields(self):
        ts = _now()
        state = DeviceState(
            device_id="battery",
            timestamp=ts,
            power_w=-2000.0,
            energy_kwh=5.4,
            soc_pct=62.5,
            available=True,
            extra={"vendor": "pylontech"},
        )
        assert state.power_w == -2000.0
        assert state.soc_pct == 62.5
        assert state.extra["vendor"] == "pylontech"

    def test_timestamp_auto_set(self):
        s1 = DeviceState(device_id="x")
        s2 = DeviceState(device_id="x")
        assert s1.timestamp <= s2.timestamp


class TestDeviceCommand:
    def test_basic(self):
        cmd = DeviceCommand(device_id="ev_charger", command="set_current", value=16)
        assert cmd.command == "set_current"
        assert cmd.value == 16

    def test_no_value(self):
        cmd = DeviceCommand(device_id="x", command="stop")
        assert cmd.value is None


class TestMeasurement:
    def test_round_trip(self):
        ts = _now()
        m = Measurement(device_id="meter", timestamp=ts, power_w=350.0, extra={"phase": "L1"})
        assert m.device_id == "meter"
        assert m.power_w == 350.0
        assert m.extra["phase"] == "L1"

    def test_optional_fields_none(self):
        m = Measurement(device_id="x", timestamp=_now())
        assert m.power_w is None
        assert m.energy_kwh is None
        assert m.soc_pct is None


class TestEnergyPlan:
    def test_empty_plan(self):
        plan = EnergyPlan()
        assert plan.actions == []
        assert plan.horizon_hours == 24
        assert isinstance(plan.created_at, datetime)

    def test_with_action(self):
        action = ControlAction(
            device_id="ev",
            command="set_current",
            value=10,
            scheduled_at=_now(),
        )
        plan = EnergyPlan(actions=[action])
        assert len(plan.actions) == 1
        assert plan.actions[0].device_id == "ev"


class TestConfigEntry:
    def test_basic(self):
        entry = ConfigEntry(id="solar", plugin="energy_manager.plugins.iobroker")
        assert entry.data == {}
        assert entry.tariff_id is None

    def test_with_tariff_id(self):
        entry = ConfigEntry(
            id="heat_pump",
            plugin="energy_manager.plugins.iobroker",
            tariff_id="waermepumpe",
        )
        assert entry.tariff_id == "waermepumpe"

    def test_with_data(self):
        entry = ConfigEntry(
            id="battery",
            plugin="energy_manager.plugins.iobroker",
            data={"host": "192.168.1.5", "port": 8087},
        )
        assert entry.data["host"] == "192.168.1.5"

    def test_serialization(self):
        entry = ConfigEntry(id="x", plugin="some.plugin", tariff_id="hauptstrom", data={"key": "val"})
        d = entry.model_dump()
        assert d["id"] == "x"
        assert d["tariff_id"] == "hauptstrom"
        assert d["data"]["key"] == "val"


class TestForecastAndTariff:
    def test_forecast_point(self):
        pt = ForecastPoint(timestamp=_now(), value=3.5)
        assert pt.value == 3.5

    def test_tariff_point(self):
        pt = TariffPoint(timestamp=_now(), price_eur_per_kwh=0.28)
        assert pt.price_eur_per_kwh == 0.28

    def test_forecast_quantity_values(self):
        assert ForecastQuantity.PRICE == "price"
        assert ForecastQuantity.PV_GENERATION == "pv_generation"
        assert ForecastQuantity.CONSUMPTION == "consumption"

"""Tests for RuleBasedOptimizer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_manager.plugins.rule_based.optimizer import RuleBasedOptimizer
from energy_manager.core.optimizer import OptimizationContext
from energy_manager.core.models import (
    DeviceCategory,
    DeviceState,
    ForecastPoint,
    ForecastQuantity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(
    device_id: str,
    category: DeviceCategory,
    power_w: float | None = None,
    soc_pct: float | None = None,
    available: bool = True,
) -> DeviceState:
    return DeviceState(
        device_id=device_id,
        timestamp=datetime.now(timezone.utc),
        power_w=power_w,
        soc_pct=soc_pct,
        available=available,
        extra={"category": category.value},
    )


def _context(
    device_states: dict[str, DeviceState],
    forecasts: dict[ForecastQuantity, list[ForecastPoint]] | None = None,
    horizon_hours: int = 4,
) -> OptimizationContext:
    return OptimizationContext(
        device_states=device_states,
        forecasts=forecasts or {},
        horizon=timedelta(hours=horizon_hours),
    )


def _pv_forecast(hourly_values_w: list[float]) -> list[ForecastPoint]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [
        ForecastPoint(timestamp=now + timedelta(hours=i), value=v)
        for i, v in enumerate(hourly_values_w)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_empty_context_returns_empty_plan() -> None:
    optimizer = RuleBasedOptimizer()
    plan = await optimizer.optimize(_context({}))
    assert plan.actions == []


async def test_no_battery_no_actions() -> None:
    optimizer = RuleBasedOptimizer()
    states = {
        "solar": _state("solar", DeviceCategory.SOURCE, power_w=3000.0),
        "ev": _state("ev", DeviceCategory.CONSUMER, power_w=-1500.0),
    }
    plan = await optimizer.optimize(_context(states))
    assert plan.actions == []


async def test_solar_surplus_schedules_battery_charge() -> None:
    optimizer = RuleBasedOptimizer()
    states = {
        "solar": _state("solar", DeviceCategory.SOURCE, power_w=3000.0),
        "ev": _state("ev", DeviceCategory.CONSUMER, power_w=-500.0),
        "battery": _state("battery", DeviceCategory.STORAGE, power_w=0.0, soc_pct=50.0),
    }
    plan = await optimizer.optimize(_context(states, horizon_hours=1))

    charge_actions = [a for a in plan.actions if a.command == "set_charge_power"]
    assert len(charge_actions) >= 1
    assert charge_actions[0].device_id == "battery"
    assert charge_actions[0].value > 0


async def test_grid_import_with_charged_battery_schedules_discharge() -> None:
    optimizer = RuleBasedOptimizer()
    states = {
        "solar": _state("solar", DeviceCategory.SOURCE, power_w=0.0),
        "heat_pump": _state("heat_pump", DeviceCategory.CONSUMER, power_w=-2000.0),
        "battery": _state("battery", DeviceCategory.STORAGE, power_w=0.0, soc_pct=80.0),
    }
    plan = await optimizer.optimize(_context(states, horizon_hours=1))

    discharge_actions = [a for a in plan.actions if a.command == "set_discharge_power"]
    assert len(discharge_actions) >= 1
    assert discharge_actions[0].value > 0


async def test_battery_below_min_soc_not_discharged() -> None:
    optimizer = RuleBasedOptimizer(min_soc_pct=20.0)
    states = {
        "solar": _state("solar", DeviceCategory.SOURCE, power_w=0.0),
        "heat_pump": _state("heat_pump", DeviceCategory.CONSUMER, power_w=-2000.0),
        "battery": _state("battery", DeviceCategory.STORAGE, power_w=0.0, soc_pct=15.0),
    }
    plan = await optimizer.optimize(_context(states, horizon_hours=1))

    discharge_actions = [a for a in plan.actions if a.command == "set_discharge_power"]
    assert discharge_actions == []


async def test_unavailable_battery_ignored() -> None:
    optimizer = RuleBasedOptimizer()
    states = {
        "solar": _state("solar", DeviceCategory.SOURCE, power_w=3000.0),
        "battery": _state("battery", DeviceCategory.STORAGE, power_w=0.0, soc_pct=50.0, available=False),
    }
    plan = await optimizer.optimize(_context(states, horizon_hours=1))
    assert plan.actions == []


async def test_plan_horizon_hours_matches_context() -> None:
    optimizer = RuleBasedOptimizer()
    plan = await optimizer.optimize(_context({}, horizon_hours=12))
    assert plan.horizon_hours == 12


async def test_pv_forecast_drives_future_slot_charge() -> None:
    optimizer = RuleBasedOptimizer()
    # No generation now, but forecast says 4 kW in slots 1–3.
    states = {
        "solar": _state("solar", DeviceCategory.SOURCE, power_w=0.0),
        "battery": _state("battery", DeviceCategory.STORAGE, power_w=0.0, soc_pct=40.0),
    }
    forecasts = {ForecastQuantity.PV_GENERATION: _pv_forecast([0, 4000, 4000, 4000])}
    plan = await optimizer.optimize(_context(states, forecasts=forecasts, horizon_hours=4))

    # Slots 1–3 should contain charge actions.
    charge_actions = [a for a in plan.actions if a.command == "set_charge_power"]
    assert len(charge_actions) >= 1


async def test_min_soc_validation() -> None:
    with pytest.raises(ValueError):
        RuleBasedOptimizer(min_soc_pct=-1)
    with pytest.raises(ValueError):
        RuleBasedOptimizer(min_soc_pct=101)


async def test_balanced_load_no_actions() -> None:
    """When source == consumer (within threshold) no actions should be emitted."""
    optimizer = RuleBasedOptimizer()
    states = {
        "solar": _state("solar", DeviceCategory.SOURCE, power_w=1000.0),
        "ev": _state("ev", DeviceCategory.CONSUMER, power_w=-1000.0),
        "battery": _state("battery", DeviceCategory.STORAGE, power_w=0.0, soc_pct=50.0),
    }
    plan = await optimizer.optimize(_context(states, horizon_hours=1))
    assert plan.actions == []

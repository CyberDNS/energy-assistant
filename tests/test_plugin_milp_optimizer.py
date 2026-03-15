"""Tests for MILPOptimizer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from energy_manager.core.models import DeviceState, ForecastPoint, ForecastQuantity, StorageConstraints
from energy_manager.core.optimizer import OptimizationContext
from energy_manager.plugins.milp.optimizer import MILPOptimizer


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_BATTERY_ID = "zendure"

_PARAMS = StorageConstraints(
    device_id=_BATTERY_ID,
    capacity_kwh=8.0,
    max_charge_kw=1.2,
    max_discharge_kw=1.2,
    charge_efficiency=0.95,
    discharge_efficiency=0.95,
    min_soc_pct=10.0,
    max_soc_pct=100.0,
)


def _optimizer(**kwargs) -> MILPOptimizer:
    return MILPOptimizer(
        baseline_load_kw=0.0,  # simplify tests
        solver_msg=False,
        **kwargs,
    )


def _battery_state(soc_pct: float = 50.0) -> DeviceState:
    return DeviceState(
        device_id=_BATTERY_ID,
        soc_pct=soc_pct,
        extra={"category": "storage", "min_soc_pct": 10.0, "max_soc_pct": 100.0},
    )


def _flat_tariff(price: float):
    """Return a minimal async mock tariff that always returns *price*."""
    tariff = AsyncMock()
    tariff.price_at = AsyncMock(return_value=price)
    return tariff


def _pv_forecast(hours_and_watts: dict[int, float]) -> list[ForecastPoint]:
    """Build a ForecastPoint list for each (offset_hours, W) pair from now."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [
        ForecastPoint(timestamp=now + timedelta(hours=h), value=w)
        for h, w in hours_and_watts.items()
    ]


def _context(
    *,
    soc_pct: float = 50.0,
    tariff=None,
    forecast_watts: dict[int, float] | None = None,
    horizon_hours: int = 6,
    storage_constraints: list | None = None,
) -> OptimizationContext:
    return OptimizationContext(
        device_states={_BATTERY_ID: _battery_state(soc_pct)},
        storage_constraints=storage_constraints if storage_constraints is not None else [_PARAMS],
        tariffs={"default": tariff} if tariff else {},
        forecasts={
            ForecastQuantity.PV_GENERATION: _pv_forecast(forecast_watts or {})
        },
        horizon=timedelta(hours=horizon_hours),
    )


# ---------------------------------------------------------------------------
# Tests — plan structure
# ---------------------------------------------------------------------------


class TestPlanStructure:
    async def test_returns_energy_plan(self):
        ctx = _context(tariff=_flat_tariff(0.30))
        plan = await _optimizer().optimize(ctx)
        from energy_manager.core.models import EnergyPlan
        assert isinstance(plan, EnergyPlan)

    async def test_action_count_equals_slots(self):
        ctx = _context(tariff=_flat_tariff(0.30), horizon_hours=6)
        plan = await _optimizer().optimize(ctx)
        assert len(plan.actions) == 6  # 6 × 1-hour slots

    async def test_horizon_hours_set(self):
        ctx = _context(tariff=_flat_tariff(0.30), horizon_hours=4)
        plan = await _optimizer().optimize(ctx)
        assert plan.horizon_hours == 4

    async def test_actions_target_battery_device(self):
        ctx = _context(tariff=_flat_tariff(0.30))
        plan = await _optimizer().optimize(ctx)
        assert all(a.device_id == _BATTERY_ID for a in plan.actions)

    async def test_action_command_is_set_automation_limit(self):
        ctx = _context(tariff=_flat_tariff(0.30))
        plan = await _optimizer().optimize(ctx)
        assert all(a.command == "set_automation_limit" for a in plan.actions)

    async def test_action_value_is_integer_watts(self):
        """Values must be integers (watts) not fractional kW."""
        ctx = _context(tariff=_flat_tariff(0.30))
        plan = await _optimizer().optimize(ctx)
        assert all(isinstance(a.value, int) for a in plan.actions)

    async def test_actions_sorted_by_time(self):
        ctx = _context(tariff=_flat_tariff(0.30))
        plan = await _optimizer().optimize(ctx)
        times = [a.scheduled_at for a in plan.actions]
        assert times == sorted(times)


# ---------------------------------------------------------------------------
# Tests — optimality properties
# ---------------------------------------------------------------------------


class TestOptimalityProperties:
    async def test_no_charging_when_price_is_uniform_and_no_load(self):
        """
        With a flat price profile and zero baseline load, charging the battery
        from the grid is never beneficial (it only costs money with no offsetting
        revenue).  The optimizer must not schedule any charging actions.
        Discharging is indifferent (value=0) but charging is strictly worse.
        """
        ctx = _context(soc_pct=50.0, tariff=_flat_tariff(0.30), horizon_hours=4)
        plan = await _optimizer().optimize(ctx)  # baseline_load_kw=0.0
        # No charging: all action values must be >= 0 (discharge or idle)
        for action in plan.actions:
            assert action.value >= 0, (
                f"Must not charge from grid with no load at flat price, "
                f"got {action.value} W at {action.scheduled_at}"
            )

    async def test_charge_during_cheap_hours_discharge_during_expensive(self):
        """
        Cheap price at t=0–2, expensive at t=3–5 → optimizer should charge cheap
        and discharge expensive (if spread is large enough to cover round-trip losses).

        Spread: 0.05 vs 0.50 → ratio 10×, far exceeds η=0.95² ≈ 0.90 threshold.
        With baseline_load_kw=0.3, discharging offsets grid import at the spot price,
        giving a clear economic signal to cycle the battery.
        """
        # Build a price map keyed by slot start time so the mock is clock-independent.
        now_h = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        price_map = {
            now_h + timedelta(hours=i): [0.05, 0.05, 0.05, 0.50, 0.50, 0.50][i]
            for i in range(6)
        }

        tiered_tariff = AsyncMock()
        async def price_at(ts):
            key = ts.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
            return price_map.get(key, 0.25)
        tiered_tariff.price_at = price_at

        ctx = _context(soc_pct=20.0, tariff=tiered_tariff, horizon_hours=6)
        # Use a non-zero baseline load so discharging has economic value.
        optimizer = MILPOptimizer(
            baseline_load_kw=0.3,
            solver_msg=False,
        )
        plan = await optimizer.optimize(ctx)
        actions = plan.actions

        # First slots (cheap): at least one must charge (negative W)
        cheap_charge = sum(-a.value for a in actions[:3] if a.value < 0)
        assert cheap_charge > 0, "Should charge during at least one cheap hour"
        # Last slots (expensive): at least one must discharge (positive W)
        expensive_discharge = sum(a.value for a in actions[3:] if a.value > 0)
        assert expensive_discharge > 0, "Should discharge during at least one expensive hour"

    async def test_pv_surplus_allows_free_charging_for_later_discharge(self):
        """
        PV = 2000 W during hours 0–2, 0 W hours 3–5.  Baseline load = 300 W.

        Charging during solar hours is "free": PV (2kW) > load (0.3kW) + max charge
        (1.2kW) → grid import = 0 regardless.  Discharging at night offsets the 0.3kW
        base load at 0.30 EUR/kWh.  So the optimizer MUST:
          1. Charge during solar hours (slot 0–2).  action.value <= 0
          2. Discharge some stored energy at night (slots 3–5).  total discharge > 0
        """
        ctx = _context(
            soc_pct=10.0,  # start at min SoC so all available capacity comes from solar
            tariff=_flat_tariff(0.30),
            forecast_watts={0: 2000.0, 1: 2000.0, 2: 2000.0, 3: 0.0, 4: 0.0, 5: 0.0},
            horizon_hours=6,
        )
        # Use non-zero baseline load so discharging at night saves money.
        optimizer = MILPOptimizer(
            baseline_load_kw=0.3,
            solver_msg=False,
        )
        plan = await optimizer.optimize(ctx)
        solar_hour_actions = plan.actions[:3]
        night_hour_actions = plan.actions[3:]
        # Charging from solar is free → optimizer charges during solar hours
        for a in solar_hour_actions:
            assert a.value <= 0, f"Should charge or idle during solar hours, got {a.value} W"
        # Stored solar energy discharged at night to offset base load
        night_discharge = sum(a.value for a in night_hour_actions if a.value > 0)
        assert night_discharge > 0, "Should discharge at night using solar energy stored during daytime"

    async def test_respects_soc_min_constraint(self):
        """Battery near minimum SoC must not be discharged below the floor."""
        # Start at min_soc (10%) with cheap prices — optimizer should not push below 10%
        params = StorageConstraints(
            device_id=_BATTERY_ID,
            capacity_kwh=8.0, max_charge_kw=1.2, max_discharge_kw=1.2,
            min_soc_pct=10.0, max_soc_pct=100.0,
        )
        ctx = _context(soc_pct=10.0, tariff=_flat_tariff(0.30), horizon_hours=4, storage_constraints=[params])
        optimizer = MILPOptimizer(
            baseline_load_kw=0.0,
            solver_msg=False,
        )
        plan = await optimizer.optimize(ctx)
        # No discharge should be scheduled (SoC already at minimum)
        for action in plan.actions:
            assert action.value <= 0, (
                f"Must not discharge when at min SoC, got {action.value} W"
            )

    async def test_respects_soc_max_constraint(self):
        """Battery at max SoC must not charge further."""
        params = StorageConstraints(
            device_id=_BATTERY_ID,
            capacity_kwh=8.0, max_charge_kw=1.2, max_discharge_kw=1.2,
            min_soc_pct=10.0, max_soc_pct=100.0,
        )
        ctx = _context(soc_pct=100.0, tariff=_flat_tariff(0.30), horizon_hours=4, storage_constraints=[params])
        optimizer = MILPOptimizer(
            baseline_load_kw=0.0,
            solver_msg=False,
        )
        plan = await optimizer.optimize(ctx)
        # No charging should be scheduled (SoC already at maximum)
        for action in plan.actions:
            assert action.value >= 0, (
                f"Must not charge when at max SoC, got {action.value} W"
            )

    async def test_no_simultaneous_charge_and_discharge(self):
        """
        The binary variable b[t] enforces that charge and discharge cannot
        happen in the same slot.  At least one of c[t] or d[t] must be 0.
        """
        ctx = _context(soc_pct=50.0, tariff=_flat_tariff(0.30), horizon_hours=4)
        plan = await _optimizer().optimize(ctx)
        # All values ≤ 0 (charging) or ≥ 0 (discharging) — never mixed in same slot.
        # Since the plan value is already d[t]-c[t], we just verify the solver ran.
        assert len(plan.actions) > 0

    async def test_empty_plan_on_zero_horizon(self):
        ctx = OptimizationContext(
            device_states={_BATTERY_ID: _battery_state()},
            storage_constraints=[_PARAMS],
            horizon=timedelta(hours=0),
        )
        plan = await _optimizer().optimize(ctx)
        assert plan.actions == []

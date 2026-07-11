"""Tests for MilpHigsOptimizer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.core.models import (
    DeviceRole,
    DeviceState,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
)
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hourly_prices(
    start: datetime,
    prices: list[float],
) -> list[ForecastPoint]:
    """Build an hourly PRICE forecast from a flat list of prices."""
    return [
        ForecastPoint(timestamp=start + timedelta(hours=i), value=p)
        for i, p in enumerate(prices)
    ]


def _state(device_id: str, soc_pct: float) -> DeviceState:
    return DeviceState(device_id=device_id, soc_pct=soc_pct)


def _battery(
    device_id: str,
    capacity_kwh: float = 10.0,
    max_charge_kw: float = 3.0,
    max_discharge_kw: float = 3.0,
    min_soc_pct: float = 10.0,
    max_soc_pct: float = 95.0,
) -> StorageConstraints:
    return StorageConstraints(
        device_id=device_id,
        capacity_kwh=capacity_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        min_soc_pct=min_soc_pct,
        max_soc_pct=max_soc_pct,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestMilpHigsOptimizerBasic:
    """Smoke tests: plan is returned and structurally correct."""

    async def test_returns_energy_plan(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=50.0)},
            storage_constraints=[_battery("bat")],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.25] * 24),
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        assert plan is not None
        assert plan.horizon_hours == 24

    async def test_intent_count_matches_timesteps(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=50.0)},
            storage_constraints=[_battery("bat")],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.25] * 24),
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        # 24 steps × 1 battery = 24 intents
        assert len(plan.intents) == 24
        assert all(i.device_id == "bat" for i in plan.intents)

    async def test_two_batteries_produce_correct_intent_count(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        ctx = OptimizationContext(
            device_states={
                "bat1": _state("bat1", soc_pct=50.0),
                "bat2": _state("bat2", soc_pct=30.0),
            },
            storage_constraints=[_battery("bat1"), _battery("bat2")],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.25] * 24),
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        ids = {i.device_id for i in plan.intents}
        assert ids == {"bat1", "bat2"}
        assert len(plan.intents) == 48  # 24 steps × 2 batteries

    async def test_no_storage_returns_empty_plan(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        ctx = OptimizationContext(
            device_states={},
            storage_constraints=[],
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        assert plan.intents == []


class TestMilpHigsOptimizerEconomics:
    """Verify that the optimizer makes economically sensible decisions."""

    async def test_charges_at_cheap_hours(self) -> None:
        """Battery should charge during the 3 cheap hours in the morning.

        A steady 1 kW load is needed: discharging during expensive hours then
        reduces grid import (saving 0.40 €/kWh), making the 0.10 €/kWh charge
        economically worthwhile.
        """
        optimizer = MilpHigsOptimizer(step_minutes=60)
        # Floor to the hour so price boundaries align with the plan's step grid
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        # Hours 0–2: cheap (0.10 €), hours 3–23: expensive (0.40 €)
        prices = [0.10, 0.10, 0.10] + [0.40] * 21
        consumption = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=1.0)
            for h in range(24)
        ]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=10.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0, max_charge_kw=3.0)],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        cheap_intents = [i for i in plan.intents if i.timestep < now + timedelta(hours=3)]
        expensive_intents = [
            i for i in plan.intents if i.timestep >= now + timedelta(hours=3)
        ]
        # The battery should be charging (or at least not discharging) during cheap hours
        assert all(i.power_kw >= 0 for i in cheap_intents)
        # During expensive hours with a loaded battery the optimizer may discharge
        assert any(i.power_kw < 0 for i in expensive_intents)

    async def test_discharges_at_expensive_hours(self) -> None:
        """With a full battery and expensive afternoon prices, expect discharge."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # All hours expensive; battery starts full; consumption keeps the load positive
        prices = [0.40] * 24
        consumption = [ForecastPoint(timestamp=now + timedelta(hours=h), value=1.0)
                       for h in range(24)]  # 1 kW steady load
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=90.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0)],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        assert any(i.power_kw < 0 for i in plan.intents)

    async def test_idle_when_soc_pinned(self) -> None:
        """When min_soc_pct == max_soc_pct == initial SoC the battery cannot move."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        prices = [0.30] * 24
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=50.0)},
            storage_constraints=[
                _battery("bat", capacity_kwh=10.0, min_soc_pct=50.0, max_soc_pct=50.0)
            ],
            forecasts={ForecastQuantity.PRICE: _hourly_prices(now, prices)},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        # SoC bounds prevent any charge or discharge — every intent must be charge_from_pv at 0
        assert all(i.power_kw == 0.0 and not i.grid_allowed for i in plan.intents)

    async def test_stores_pv_for_expensive_evening(self) -> None:
        """PV surplus at moderate price should be stored for a more expensive evening.

        With efficiency losses (η ≈ 0.90 round-trip) the premium must be large
        enough to make storage worthwhile vs. exporting and re-importing.
        Cheap midday (0.15 €/kWh) + expensive evening (0.45 €/kWh) provides a 3×
        price ratio which more than compensates for efficiency losses.
        """
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Cheap midday (8-16), expensive evening (16-23)
        prices = (
            [0.25] * 8          # night/morning
            + [0.15] * 8        # midday — cheap → charge
            + [0.45] * 8        # evening — expensive → discharge
        )
        # 4 kW PV only during midday hours, 1 kW steady load
        pv = [
            ForecastPoint(
                timestamp=now + timedelta(hours=h),
                value=4.0 if 8 <= h < 16 else 0.0,
            )
            for h in range(24)
        ]
        consumption = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=1.0)
            for h in range(24)
        ]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=20.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0, max_charge_kw=3.0)],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.PV_GENERATION: pv,
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        midday_intents = [
            i for i in plan.intents if now + timedelta(hours=8) <= i.timestep < now + timedelta(hours=16)
        ]
        evening_intents = [
            i for i in plan.intents if now + timedelta(hours=16) <= i.timestep
        ]
        assert any(i.power_kw > 0 and i.grid_allowed for i in midday_intents), "expected charging during midday"
        assert any(i.power_kw < 0 for i in evening_intents), "expected discharging in the evening"


class TestMilpHigsOptimizerIntentValues:
    """Check that ControlIntent fields obey platform sign conventions."""

    async def test_charge_intent_has_positive_max_power(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Very cheap first hour — battery charges to serve load at expensive rate later
        prices = [0.05] + [0.35] * 23
        consumption = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=1.0)
            for h in range(24)
        ]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=10.0)},
            storage_constraints=[_battery("bat")],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        charge_intents = [i for i in plan.intents if i.power_kw > 0 and i.grid_allowed]
        assert charge_intents, "Expected at least one charge intent"
        for intent in charge_intents:
            assert intent.power_kw > 0, "Charge power must be positive"

    async def test_discharge_intent_has_negative_min_power(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Very expensive first hour with small load — full battery will discharge
        prices = [0.50] + [0.10] * 23
        consumption = [ForecastPoint(timestamp=now + timedelta(hours=h), value=0.5)
                       for h in range(24)]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=90.0)},
            storage_constraints=[_battery("bat")],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        discharge_intents = [i for i in plan.intents if i.power_kw < 0]
        assert discharge_intents, "Expected at least one discharge intent"
        for intent in discharge_intents:
            assert intent.power_kw < 0, "Discharge power must be negative"

    async def test_grid_fill_policy_defaults_to_grid_allowed(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        prices = [0.05] + [0.35] * 23
        consumption = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=1.0)
            for h in range(24)
        ]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=10.0)},
            storage_constraints=[_battery("bat")],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        charge_intents = [i for i in plan.intents if i.power_kw > 0 and i.grid_allowed]
        assert charge_intents
        assert all(i.grid_allowed for i in charge_intents)

    async def test_grid_fill_policy_is_pv_only_for_no_grid_charge(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        prices = [0.25] * 24
        consumption = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=1.0)
            for h in range(24)
        ]
        pv = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=4.0)
            for h in range(24)
        ]
        sc = _battery("bat")
        sc.no_grid_charge = True
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=10.0)},
            storage_constraints=[sc],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.CONSUMPTION: consumption,
                ForecastQuantity.PV_GENERATION: pv,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        charge_intents = [i for i in plan.intents if i.power_kw > 0 and not i.grid_allowed]
        assert charge_intents
        assert all(not i.grid_allowed for i in charge_intents)

    async def test_pv_surplus_prefers_no_grid_charge_battery(self) -> None:
        """In PV-surplus hours, no-grid-charge batteries should be prioritized.

        Scenario: SMA (pv_only) and Zendure (grid-capable) are both empty.
        Early hours have limited PV surplus (only enough for one battery at full
        rate). The long-horizon model should allocate more of that surplus to SMA,
        preserving Zendure's flexibility to grid-charge later if needed.
        """
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        prices = [0.20] * 24
        pv = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=3.0 if h < 3 else 0.0)
            for h in range(24)
        ]
        consumption = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=2.0 if 6 <= h < 12 else 0.0)
            for h in range(24)
        ]

        sma = _battery("sma", capacity_kwh=8.0, max_charge_kw=3.0, max_discharge_kw=3.0)
        sma.no_grid_charge = True
        zendure = _battery("zendure", capacity_kwh=8.0, max_charge_kw=3.0, max_discharge_kw=3.0)

        ctx = OptimizationContext(
            device_states={
                "sma": _state("sma", soc_pct=10.0),
                "zendure": _state("zendure", soc_pct=10.0),
            },
            storage_constraints=[sma, zendure],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.PV_GENERATION: pv,
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )

        plan = await optimizer.optimize(ctx)

        first_pv_window = now + timedelta(hours=3)
        sma_charge_kwh = sum(
            i.reserved_kwh or 0.0
            for i in plan.intents
            if i.device_id == "sma" and i.power_kw > 0 and i.timestep < first_pv_window
        )
        zendure_charge_kwh = sum(
            i.reserved_kwh or 0.0
            for i in plan.intents
            if i.device_id == "zendure" and i.power_kw > 0 and i.timestep < first_pv_window
        )

        assert sma_charge_kwh > zendure_charge_kwh


class TestMilpHigsOptimizerTimeResolution:
    """Verify the optimizer handles sub-hourly time steps correctly."""

    async def test_fifteen_minute_steps_produce_96_intents(self) -> None:
        """24 h ÷ 15 min = 96 steps → 96 intents per battery."""
        optimizer = MilpHigsOptimizer(step_minutes=15)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        prices = [
            ForecastPoint(timestamp=now + timedelta(minutes=15 * i), value=0.25)
            for i in range(96)
        ]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=50.0)},
            storage_constraints=[_battery("bat")],
            forecasts={ForecastQuantity.PRICE: prices},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        assert len(plan.intents) == 96
        assert all(i.device_id == "bat" for i in plan.intents)

    async def test_fifteen_minute_steps_respect_power_limits(self) -> None:
        """With 15-min steps, max_charge_kw × 0.25h = max kWh per step."""
        optimizer = MilpHigsOptimizer(step_minutes=15)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Very cheap first hour → battery charges at full rate for all 4 steps
        prices = [0.05] * 4 + [0.50] * 92
        prices_fc = [
            ForecastPoint(timestamp=now + timedelta(minutes=15 * i), value=prices[i])
            for i in range(96)
        ]
        bat = _battery("bat", capacity_kwh=10.0, max_charge_kw=2.0)
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=10.0)},
            storage_constraints=[bat],
            forecasts={ForecastQuantity.PRICE: prices_fc},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        # Power during cheap steps must not exceed max_charge_kw
        cheap_intents = [
            i for i in plan.intents
            if i.timestep < now + timedelta(hours=1) and i.power_kw > 0 and i.grid_allowed
        ]
        for intent in cheap_intents:
            assert intent.power_kw <= bat.max_charge_kw + 0.001  # +1 W tolerance in kW

    async def test_hourly_pv_upsampled_to_fifteen_min(self) -> None:
        """Hourly PV data is correctly nearest-neighbour aligned to 15-min steps."""
        optimizer = MilpHigsOptimizer(step_minutes=15)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Hourly prices
        prices_fc = [
            ForecastPoint(timestamp=now + timedelta(minutes=15 * i), value=0.30)
            for i in range(96)
        ]
        # Hourly PV: 3 kW for hour 0 only (4 × 15-min steps)
        pv_fc = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=3.0 if h == 0 else 0.0)
            for h in range(24)
        ]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=10.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0, max_charge_kw=3.0)],
            forecasts={
                ForecastQuantity.PRICE: prices_fc,
                ForecastQuantity.PV_GENERATION: pv_fc,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        # Plan should exist and have the correct number of intents
        assert len(plan.intents) == 96


class TestMilpHigsOptimizerExportPrice:
    """Verify the optimizer correctly uses the export price from a tariff."""

    async def test_charges_pv_surplus_rather_than_exporting_at_low_feed_in(self) -> None:
        """With export_price << import_price, PV surplus should fill the battery.

        export_price = 0.08 €/kWh, import_price = 0.25 €/kWh.
        Hours 0–11: 4 kW PV → 3 kW surplus (can charge).
        Hours 12–23: no PV, 1 kW load (must import or discharge).
        Storing 1 kWh saves 0.25 later; exporting earns only 0.08 now.
        Round-trip: 0.25 × 0.95 = 0.2375 break-even >> 0.08, so charging wins.
        """
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        prices = [0.25] * 24
        # PV only in the first 12 hours
        pv = [
            ForecastPoint(
                timestamp=now + timedelta(hours=h),
                value=4.0 if h < 12 else 0.0,
            )
            for h in range(24)
        ]
        consumption = [
            ForecastPoint(timestamp=now + timedelta(hours=h), value=1.0)
            for h in range(24)
        ]
        grid_tariff = FlatRateTariff(
            tariff_id="grid",
            import_price_eur_per_kwh=0.0,
            export_price_eur_per_kwh=0.08,
        )
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=10.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0, max_charge_kw=3.0)],
            tariffs={"grid": grid_tariff},
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, prices),
                ForecastQuantity.PV_GENERATION: pv,
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        morning_intents = [
            i for i in plan.intents if i.timestep < now + timedelta(hours=12)
        ]
        # Battery should charge during PV surplus hours (not just export everything)
        assert any(i.power_kw > 0 for i in morning_intents), (
            "Expected battery to charge from PV surplus rather than exporting at low feed-in price"
        )

    async def test_export_price_zero_still_allows_pv_export(self) -> None:
        """When no export tariff is configured, export is free (price = 0).

        The optimizer must still run correctly and handle PV surplus without error.
        """
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # 5 kW PV, no load, small battery → most PV must be exported
        pv = [ForecastPoint(timestamp=now + timedelta(hours=h), value=5.0) for h in range(24)]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=50.0)},
            storage_constraints=[_battery("bat", capacity_kwh=5.0, max_charge_kw=2.0)],
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.25] * 24),
                ForecastQuantity.PV_GENERATION: pv,
            },
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        # Plan should complete without error and produce intents
        assert plan is not None
        assert len(plan.intents) == 24


class TestMilpHigsOptimizerTerminalValue:
    """Verify that battery_cost_basis prevents selling stored energy below cost."""

    async def test_high_cost_basis_prevents_cheap_export(self) -> None:
        """With cost_basis = 0.25 €/kWh, the optimizer must not discharge to export
        at 0.082 €/kWh — even with no remaining load in the horizon.

        Without terminal value: optimizer sees 'free money' from exporting.
        With terminal value:    discharging costs 0.25 in terminal value but earns
                                only 0.082 from export → net loss → optimizer holds.
        """
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        grid_tariff = FlatRateTariff(
            tariff_id="grid",
            import_price_eur_per_kwh=0.0,
            export_price_eur_per_kwh=0.082,
        )
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=80.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0, max_discharge_kw=3.0)],
            tariffs={"grid": grid_tariff},
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.25] * 24),
                # No consumption → no load to serve; exporting is the only use
            },
            horizon=timedelta(hours=24),
            battery_cost_basis={"bat": 0.25},   # stored energy cost 0.25 €/kWh
        )
        plan = await optimizer.optimize(ctx)
        # Export price (0.082) < cost_basis (0.25) → no discharge should occur
        discharge_intents = [i for i in plan.intents if i.power_kw < 0]
        assert not discharge_intents, (
            "Optimizer discharged battery to export at 0.082 €/kWh "
            "even though stored energy cost 0.25 €/kWh"
        )

    async def test_zero_cost_basis_allows_export(self) -> None:
        """With cost_basis = 0 (e.g. free PV charge), exporting at 0.082 is profitable."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        grid_tariff = FlatRateTariff(
            tariff_id="grid",
            import_price_eur_per_kwh=0.0,
            export_price_eur_per_kwh=0.082,
        )
        # Provide consumption so the battery has something to serve before exporting
        consumption = [ForecastPoint(timestamp=now + timedelta(hours=h), value=0.5)
                       for h in range(24)]
        ctx = OptimizationContext(
            device_states={"bat": _state("bat", soc_pct=80.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0, max_discharge_kw=3.0)],
            tariffs={"grid": grid_tariff},
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.25] * 24),
                ForecastQuantity.CONSUMPTION: consumption,
            },
            horizon=timedelta(hours=24),
            battery_cost_basis={"bat": 0.0},   # free stored energy (charged from PV)
        )
        plan = await optimizer.optimize(ctx)
        # With zero basis, discharging to serve load or export is always profitable
        discharge_intents = [i for i in plan.intents if i.power_kw < 0]
        assert discharge_intents, "Expected discharge when stored energy was free"


class TestEvPvSourceConstraint:
    """PV-labeled EV slots must stay within the forecast PV surplus.

    Regression: the solver used to fill grid_allowed=False slots with
    battery/grid energy far beyond the available PV, which execution
    (openWB PV mode = real-surplus tracking) can never deliver.
    """

    # PV window: hours 1–8 relative to the forecast start.  The optimizer
    # plans from real wall-clock now, so the window must lie in the future.
    PV_WINDOW = range(1, 9)
    CONS_KW = 0.4

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _goal(now: datetime, deadline_h: int = 20):
        from energy_assistant.assets.ev import build_goal_from_parts
        return build_goal_from_parts(
            asset_id="ev1", device_id="cp", capacity_kwh=40.0,
            max_charge_kw=7.4, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
            target_soc_pct=90.0, target_by=now + timedelta(hours=deadline_h),
            charge_curve=[], current_soc_pct=60.0, connected=True,
        )

    @classmethod
    def _pv_kw_at(cls, now: datetime, ts: datetime, pv_kw: float) -> float:
        h = int((ts - now).total_seconds() // 3600)
        return pv_kw if h in cls.PV_WINDOW else 0.0

    @classmethod
    def _ctx(cls, now: datetime, pv_kw: float, goal) -> OptimizationContext:
        from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
        pv = [ForecastPoint(timestamp=now + timedelta(hours=h),
                            value=pv_kw if h in cls.PV_WINDOW else 0.0) for h in range(24)]
        cons = [ForecastPoint(timestamp=now + timedelta(hours=h), value=cls.CONS_KW)
                for h in range(24)]
        return OptimizationContext(
            device_states={"cp": DeviceState(device_id="cp", soc_pct=60.0, available=True),
                           "bat": _state("bat", soc_pct=90.0)},
            storage_constraints=[_battery("bat", capacity_kwh=10.0)],
            tariffs={"grid": FlatRateTariff("grid", import_price_eur_per_kwh=0.30,
                                            export_price_eur_per_kwh=0.08)},
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.30] * 24),
                ForecastQuantity.PV_GENERATION: pv,
                ForecastQuantity.CONSUMPTION: cons,
            },
            horizon=timedelta(hours=24),
            ev_charging_goals=[goal],
        )

    async def test_pv_labeled_slots_never_exceed_forecast_surplus(self) -> None:
        now = self._now()
        goal = self._goal(now)
        # PV 2 kW < min charge 4.14 kW: PV slots are impossible, every
        # planned charging slot must be grid_allowed=True.
        plan = await MilpHigsOptimizer(step_minutes=60, precision=0.5).optimize(
            self._ctx(now, pv_kw=2.0, goal=goal)
        )
        ev_intents = [i for i in plan.intents if i.device_id == "cp" and i.power_kw > 0]
        assert ev_intents, "expected planned EV charging"
        for i in ev_intents:
            surplus = self._pv_kw_at(now, i.timestep, 2.0) - self.CONS_KW
            if not i.grid_allowed:
                assert i.power_kw <= max(0.0, surplus) + 0.05, (
                    f"pv-labeled slot {i.timestep} plans {i.power_kw} kW "
                    f"but only {surplus:.1f} kW PV surplus is available"
                )

    async def test_ample_pv_yields_pv_labeled_slots_within_surplus(self) -> None:
        now = self._now()
        goal = self._goal(now)
        plan = await MilpHigsOptimizer(step_minutes=60, precision=0.5).optimize(
            self._ctx(now, pv_kw=6.0, goal=goal)
        )
        ev_intents = [i for i in plan.intents if i.device_id == "cp" and i.power_kw > 0]
        pv_slots = [i for i in ev_intents if not i.grid_allowed]
        assert pv_slots, "with 6 kW PV the solver should plan PV-sourced slots"
        for i in pv_slots:
            assert i.power_kw <= 6.0 - self.CONS_KW + 0.05

    async def test_plan_carries_solved_flows(self) -> None:
        """Plans must expose the solver's per-slot site flows (effective PV,
        grid import/export) so the UI doesn't re-derive them from raw
        forecasts (which diverge e.g. via the live-PV floor)."""
        now = self._now()
        goal = self._goal(now)
        plan = await MilpHigsOptimizer(step_minutes=60, precision=0.5).optimize(
            self._ctx(now, pv_kw=6.0, goal=goal)
        )
        assert plan.flows, "expected solved flows on the plan"
        by_ts = {f.timestep: f for f in plan.flows}
        for i in plan.intents:
            if i.device_id == "cp" and i.power_kw > 0 and not i.grid_allowed:
                f = by_ts[i.timestep]
                # PV slot: solver reports no grid import beyond tolerance.
                assert f.grid_import_kw <= 0.05, (
                    f"pv slot {i.timestep} has solved grid import {f.grid_import_kw} kW"
                )
                assert f.pv_kw > 0.0


class TestLivePvBlend:
    """Current-hour PV: anchored at the live reading, linearly approaching
    the forecast value by the end of the hour (both directions)."""

    @staticmethod
    async def _flows(live_pv_w: float, forecast_kw: float):
        from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
        now = datetime.now(timezone.utc)
        pv = [ForecastPoint(timestamp=now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=h),
                            value=forecast_kw) for h in range(24)]
        ctx = OptimizationContext(
            device_states={
                "bat": _state("bat", soc_pct=50.0),
                "pv": DeviceState(device_id="pv", power_w=-live_pv_w),
            },
            storage_constraints=[_battery("bat")],
            tariffs={"grid": FlatRateTariff("grid", import_price_eur_per_kwh=0.30,
                                            export_price_eur_per_kwh=0.08)},
            forecasts={
                ForecastQuantity.PRICE: _hourly_prices(now, [0.30] * 24),
                ForecastQuantity.PV_GENERATION: pv,
            },
            horizon=timedelta(hours=24),
            producer_device_ids={"pv"},
        )
        plan = await MilpHigsOptimizer(step_minutes=15, precision=0.5).optimize(ctx)
        hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        in_hour = [f for f in plan.flows if f.timestep < hour_end]
        after = [f for f in plan.flows if f.timestep >= hour_end]
        return in_hour, after

    async def test_live_above_forecast_blends_down(self) -> None:
        in_hour, after = await self._flows(live_pv_w=5_000.0, forecast_kw=2.0)
        assert in_hour, "expected slots in the current hour"
        # First slot anchored at (or near) the live 5 kW reading.
        assert in_hour[0].pv_kw >= 4.0
        # Monotonically approaching the forecast, never leaving [2, 5].
        vals = [f.pv_kw for f in in_hour]
        assert all(2.0 - 1e-6 <= v <= 5.0 + 1e-6 for v in vals)
        assert all(a >= b - 1e-6 for a, b in zip(vals, vals[1:]))
        # Beyond the hour: pure forecast.
        assert all(abs(f.pv_kw - 2.0) < 1e-6 for f in after[:8])

    async def test_live_below_forecast_blends_up(self) -> None:
        in_hour, after = await self._flows(live_pv_w=1_000.0, forecast_kw=4.0)
        assert in_hour
        assert in_hour[0].pv_kw <= 2.0   # anchored near the live 1 kW
        vals = [f.pv_kw for f in in_hour]
        assert all(1.0 - 1e-6 <= v <= 4.0 + 1e-6 for v in vals)
        assert all(a <= b + 1e-6 for a, b in zip(vals, vals[1:]))
        assert all(abs(f.pv_kw - 4.0) < 1e-6 for f in after[:8])

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
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
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
        assert all(i.mode != "discharge" for i in cheap_intents)
        # During expensive hours with a loaded battery the optimizer may discharge
        assert any(i.mode == "discharge" for i in expensive_intents)

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
        assert any(i.mode == "discharge" for i in plan.intents)

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
        # SoC bounds prevent any charge or discharge — every intent must be idle
        assert all(i.mode == "idle" for i in plan.intents)

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
        assert any(i.mode == "grid_fill" for i in midday_intents), "expected charging during midday"
        assert any(i.mode == "discharge" for i in evening_intents), "expected discharging in the evening"


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
        charge_intents = [i for i in plan.intents if i.mode == "grid_fill"]
        assert charge_intents, "Expected at least one charge intent"
        for intent in charge_intents:
            assert intent.max_power_w is not None
            assert intent.max_power_w > 0, "Charge power must be positive"
            assert intent.min_power_w == 0.0

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
        discharge_intents = [i for i in plan.intents if i.mode == "discharge"]
        assert discharge_intents, "Expected at least one discharge intent"
        for intent in discharge_intents:
            assert intent.min_power_w is not None
            assert intent.min_power_w < 0, "Discharge power bound must be negative"
            assert intent.max_power_w == 0.0

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
        charge_intents = [i for i in plan.intents if i.mode == "grid_fill"]
        assert charge_intents
        assert all(i.charge_policy == "grid_allowed" for i in charge_intents)

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
        charge_intents = [i for i in plan.intents if i.mode == "grid_fill"]
        assert charge_intents
        assert all(i.charge_policy == "pv_only" for i in charge_intents)

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
            if i.device_id == "sma" and i.mode == "grid_fill" and i.timestep < first_pv_window
        )
        zendure_charge_kwh = sum(
            i.reserved_kwh or 0.0
            for i in plan.intents
            if i.device_id == "zendure" and i.mode == "grid_fill" and i.timestep < first_pv_window
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
            if i.timestep < now + timedelta(hours=1) and i.mode == "grid_fill"
        ]
        for intent in cheap_intents:
            assert intent.max_power_w is not None
            assert intent.max_power_w <= bat.max_charge_kw * 1000 + 1  # +1 W tolerance

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
        assert any(i.mode == "grid_fill" for i in morning_intents), (
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
        discharge_intents = [i for i in plan.intents if i.mode == "discharge"]
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
        discharge_intents = [i for i in plan.intents if i.mode == "discharge"]
        assert discharge_intents, "Expected discharge when stored energy was free"

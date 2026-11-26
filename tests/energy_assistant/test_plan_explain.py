"""Plan explanations: shadow prices and reasons per slot (milp_highs/explain.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from energy_assistant.core.models import (
    DeviceState,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
    ThresholdConstraints,
)
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer
from energy_assistant.plugins.milp_highs import optimizer as optimizer_module


def _now() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def _series(now: datetime, values: list[float]) -> list[ForecastPoint]:
    return [ForecastPoint(timestamp=now + timedelta(hours=h), value=v) for h, v in enumerate(values)]


def _battery(**kw) -> StorageConstraints:
    args = {
        "device_id": "bat", "capacity_kwh": 10.0, "max_charge_kw": 3.0, "max_discharge_kw": 3.0,
        "min_soc_pct": 10.0, "max_soc_pct": 95.0,
    }
    args.update(kw)
    return StorageConstraints(**args)


# Cheap night, expensive morning, medium day, expensive evening.
PRICES = [0.10] * 3 + [0.40] * 5 + [0.20] * 8 + [0.45] * 4 + [0.25] * 4


def _ctx(now: datetime, prices=PRICES, **kw) -> OptimizationContext:
    args = {
        "device_states": {"bat": DeviceState(device_id="bat", soc_pct=30.0)},
        "storage_constraints": [_battery()],
        "forecasts": {
            ForecastQuantity.PRICE: _series(now, prices),
            ForecastQuantity.CONSUMPTION: _series(now, [1.0] * 24),
        },
        "horizon": timedelta(hours=24),
    }
    args.update(kw)
    return OptimizationContext(**args)


def _by_slot(plan, device_id: str):
    return sorted((i for i in plan.intents if i.device_id == device_id), key=lambda i: i.timestep)


async def test_every_battery_slot_has_a_reason_and_an_energy_value() -> None:
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(_now()))
    intents = _by_slot(plan, "bat")
    assert len(intents) == 24
    assert all(i.reason for i in intents)
    assert all(i.energy_value_eur_per_kwh is not None for i in intents)
    assert all(f.marginal_price_eur_per_kwh is not None for f in plan.flows)


async def test_marginal_price_is_the_import_price_while_importing() -> None:
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(_now()))
    for f in plan.flows:
        if f.grid_import_kw > 0.01:
            assert f.marginal_price_eur_per_kwh == pytest.approx(f.import_price, abs=1e-3)


async def test_charge_reason_names_the_price_and_the_later_use() -> None:
    now = _now()
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(now))
    first = _by_slot(plan, "bat")[0]
    assert first.power_kw > 0 and first.grid_allowed
    assert first.reason.startswith("Charging from grid at €0.10/kWh for use from ")
    use = (now + timedelta(hours=3)).astimezone().strftime("%H:%M")
    assert f"from {use} (€0.40/kWh)" in first.reason
    # 0.95 × 0.95 × 0.40 − 0.10 ≈ €0.26 per kWh charged.
    assert "gain €0.26/kWh after losses and wear" in first.reason


async def test_power_limit_is_a_binding_constraint() -> None:
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(_now()))
    full_power = [i for i in _by_slot(plan, "bat") if i.power_kw >= 3.0 - 1e-6]
    assert full_power
    assert all("max_charge_power" in i.binding_constraints for i in full_power)
    assert all("Limited by: max charge 3.0 kW" in i.reason for i in full_power)


async def test_discharge_and_hold_compare_with_the_stored_energy_value() -> None:
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(_now()))
    intents = _by_slot(plan, "bat")
    discharging = [i for i in intents if i.power_kw < 0]
    assert discharging
    for i in discharging:
        assert i.reason.startswith("Discharging instead of buying grid energy at €0.4")
        assert "/ 0.95 efficiency" in i.reason
        # Covers only the 1 kW house load: no feed-in tariff.
        assert "house_load" in i.binding_constraints
    holding = [i for i in intents[8:16] if abs(i.power_kw) < 1e-6 and "Holding" in i.reason]
    assert holding, [i.reason for i in intents[8:16]]
    assert "discharging now would save only €0.20/kWh" in holding[0].reason


async def test_explanation_does_not_change_the_plan(monkeypatch) -> None:
    now = _now()
    explained = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(now))
    monkeypatch.setattr(optimizer_module, "solve_duals", lambda prob: None)
    plain = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(now))
    assert [i.power_kw for i in explained.intents] == [i.power_kw for i in plain.intents]
    assert [i.stored_energy_kwh for i in explained.intents] == [
        i.stored_energy_kwh for i in plain.intents
    ]
    # Without duals the plan simply carries no reasons.
    assert all(i.reason is None and i.binding_constraints == [] for i in plain.intents)
    assert all(f.marginal_price_eur_per_kwh is None for f in plain.flows)


async def test_failing_explanation_never_fails_the_plan(monkeypatch) -> None:
    def boom(duals, inputs):
        raise ValueError("bug in a template")

    monkeypatch.setattr(optimizer_module, "explain", boom)
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(_ctx(_now()))
    assert len(plan.intents) == 24
    assert all(i.reason is None for i in plan.intents)


async def test_grid_limit_shows_as_binding_and_raises_the_marginal_price() -> None:
    now = _now()
    batteries = [_battery(device_id=d) for d in ("a", "b")]
    ctx = _ctx(
        now,
        prices=[0.40, 0.05, 0.05] + [0.50] * 21,
        device_states={d: DeviceState(device_id=d, soc_pct=10.0) for d in ("a", "b")},
        storage_constraints=batteries,
        max_import_kw=4.0,
    )
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(ctx)
    limited = [i for i in plan.intents if "grid_limit" in i.binding_constraints]
    assert limited
    flows = {f.timestep: f for f in plan.flows}
    for i in limited:
        f = flows[i.timestep]
        assert f.marginal_price_eur_per_kwh > f.import_price + 0.005
        assert "grid connection limit" in i.reason


async def test_pv_charge_up_to_full() -> None:
    now = _now()
    pv = [0, 0, 4, 6, 6, 6, 6, 4, 0] + [0] * 15
    ctx = _ctx(
        now,
        prices=[0.30] * 24,
        device_states={"bat": DeviceState(device_id="bat", soc_pct=50.0)},
        storage_constraints=[_battery(max_soc_pct=100.0)],
        tariffs={"grid": FlatRateTariff("grid", import_price_eur_per_kwh=0.30,
                                        export_price_eur_per_kwh=0.08)},
        forecasts={
            ForecastQuantity.PRICE: _series(now, [0.30] * 24),
            ForecastQuantity.CONSUMPTION: _series(now, [0.5] * 24),
            ForecastQuantity.PV_GENERATION: _series(now, pv),
        },
    )
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(ctx)
    intents = _by_slot(plan, "bat")
    pv_charge = [i for i in intents if i.power_kw > 0]
    assert pv_charge and not any(i.grid_allowed for i in pv_charge)
    assert all(
        i.reason.startswith("Storing PV surplus instead of feeding it in at €0.08/kWh")
        for i in pv_charge
    )
    filled = [i for i in pv_charge if "max_soc" in i.binding_constraints]
    assert filled and "max SoC 100%" in filled[0].reason


async def test_ev_reasons() -> None:
    from energy_assistant.assets.ev import build_goal_from_parts

    now = _now()
    goal = build_goal_from_parts(
        asset_id="ev1", device_id="cp", capacity_kwh=40.0, max_charge_kw=7.4,
        min_charge_kw=4.14, charge_limit_soc_pct=80.0, target_soc_pct=90.0,
        target_by=now + timedelta(hours=12), charge_curve=[], current_soc_pct=60.0,
        connected=True,
    )
    ctx = _ctx(
        now,
        prices=[0.30] * 4 + [0.15] * 4 + [0.30] * 16,
        device_states={"cp": DeviceState(device_id="cp", soc_pct=60.0),
                       "bat": DeviceState(device_id="bat", soc_pct=50.0)},
        ev_charging_goals=[goal],
    )
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(ctx)
    ev = _by_slot(plan, "cp")
    assert ev and all(i.reason for i in ev)
    phase1 = [i for i in ev if i.timestep < goal.phase2_start_time - timedelta(hours=1)]
    assert phase1
    assert all(i.reason.startswith("Charging from grid at €0.15/kWh") for i in phase1)
    assert all("towards 80%" in i.reason for i in phase1)
    mandatory = [i for i in ev if "ev_mandatory" in i.binding_constraints]
    assert mandatory
    assert mandatory[-1].reason.startswith("Instant charging from ")
    assert "to reach 90% by " in mandatory[-1].reason


async def test_threshold_reasons() -> None:
    now = _now()
    tc = ThresholdConstraints(
        device_id="cooler", bottom_threshold=24.0, top_threshold=28.0, unit="°C",
        direction="reduces", rated_power_kw=0.2, active_rate_per_h=2.0, drift_rate_per_h=1.0,
    )
    ctx = _ctx(
        now,
        device_states={"bat": DeviceState(device_id="bat", soc_pct=50.0),
                       "cooler": DeviceState(device_id="cooler", extra={"measured_value": 26.0})},
        threshold_constraints=[tc],
    )
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(ctx)
    reasons = [i.reason for i in _by_slot(plan, "cooler")]
    assert all(reasons)
    assert any(r.startswith("Running at €") and "within 24–28°C" in r for r in reasons)
    assert any(r.startswith("Off: predicted") for r in reasons)

"""Power-dependent grid charges (issue #105): config, meter, planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nornwatt.core.control import (
    ControlLoop,
    LiveSituation,
    StorageControlContributor,
)
from nornwatt.core.ledger import BatteryCostLedger
from nornwatt.core.models import (
    ControlIntent,
    DeviceState,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
)
from nornwatt.core.optimizer import OptimizationContext
from nornwatt.core.power_charge import (
    MonthlyPeak,
    PowerChargeContext,
    PowerChargeMeter,
    PowerCharges,
    PowerThreshold,
    interval_start,
    month_key,
    parse_power_charges,
    peak_from_history,
    validate_tariffs,
)
from nornwatt.loader.config_validation import validate_sections
from nornwatt.plugins.milp_highs import MilpHigsOptimizer

# ── Config ────────────────────────────────────────────────────────────────────


def test_parse_both_kinds() -> None:
    charges, errors = parse_power_charges([
        {"type": "power_threshold", "threshold_kw": 7, "excess_eur_per_kwh": 0.11},
        {"type": "monthly_peak", "eur_per_kw_month": 4.2, "minimum_kw": 2.5},
    ])
    assert errors == []
    assert charges.thresholds == (PowerThreshold(7.0, 0.11),)
    assert charges.peaks == (MonthlyPeak(4.2, 2.5),)


@pytest.mark.parametrize(("raw", "fragment"), [
    ("x", "must be a list"),
    ([{"type": "other"}], "type must be"),
    ([{"type": "power_threshold", "threshold_kw": 7}], "excess_eur_per_kwh is required"),
    ([{"type": "power_threshold", "threshold_kw": 0, "excess_eur_per_kwh": 0.1}], "> 0"),
    ([{"type": "monthly_peak", "eur_per_kw_month": "a lot"}], "numbers"),
])
def test_parse_errors(raw, fragment: str) -> None:
    _, errors = parse_power_charges(raw)
    assert any(fragment in e for e in errors), errors


def test_only_the_default_tariff_may_have_power_charges() -> None:
    pc = [{"type": "monthly_peak", "eur_per_kw_month": 4.2}]
    tariffs = {"grid": {"type": "flat_rate", "power_charges": pc},
               "heat": {"type": "flat_rate", "power_charges": pc}}
    errors = validate_tariffs(tariffs, "grid")
    assert len(errors) == 1 and errors[0].startswith("tariffs: heat: power_charges")


def test_costs() -> None:
    charges = PowerCharges((PowerThreshold(7.0, 0.10),), (MonthlyPeak(4.0, 2.5),))
    assert charges.interval_cost(2.0) == pytest.approx((2.0 - 1.75) * 0.10)
    assert charges.interval_cost(1.0) == 0.0
    assert charges.peak_cost(0.0, 2.0) == 0.0               # below the minimum
    assert charges.peak_cost(2.0, 5.0) == pytest.approx(2.5 * 4.0)
    assert charges.peak_cost(5.0, 4.0) == 0.0
    assert charges.free_kw(0.0) == 2.5
    assert charges.free_kw(9.0) == 7.0


# ── Meter ─────────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def test_meter_closes_intervals_and_tracks_the_peak() -> None:
    m = PowerChargeMeter()
    assert m.add(_T0, _T0 + timedelta(minutes=10), 6.0) == []
    closed = m.add(_T0 + timedelta(minutes=10), _T0 + timedelta(minutes=20), 3.0)
    assert len(closed) == 1
    # 10 min at 6 kW + 5 min at 3 kW = 1.25 kWh → 5 kW average
    assert closed[0].average_kw == pytest.approx(5.0)
    assert closed[0].old_peak_kw == 0.0 and closed[0].new_peak_kw == pytest.approx(5.0)
    assert m.interval_import_kwh(_T0 + timedelta(minutes=20)) == pytest.approx(0.25)
    # Ending exactly on a boundary closes the interval right away.
    closed = m.add(_T0 + timedelta(minutes=20), _T0 + timedelta(minutes=30), 1.0)
    assert len(closed) == 1 and closed[0].new_peak_kw == pytest.approx(5.0)
    assert m.month_peak_kw(_T0) == pytest.approx(5.0)
    # Round trip through the persisted form.
    assert PowerChargeMeter.from_dict(m.to_dict()) == m


def test_meter_starts_a_new_month_at_zero() -> None:
    m = PowerChargeMeter()
    end = datetime(2026, 11, 30, 23, 0).astimezone()   # local time
    m.add(end - timedelta(minutes=15), end, 8.0)
    assert m.month_peak_kw(end - timedelta(minutes=1)) == pytest.approx(8.0)
    nxt = end + timedelta(days=1, hours=2)
    assert m.month_peak_kw(nxt) == 0.0
    closed = m.add(nxt, nxt + timedelta(minutes=15), 2.0)
    assert closed[-1].old_peak_kw == 0.0
    assert m.month == month_key(nxt) and m.peak_kw == pytest.approx(2.0)


def test_allowed_import_spreads_the_rest_of_the_budget() -> None:
    charges = PowerCharges((PowerThreshold(4.0, 0.1),))
    m = PowerChargeMeter()
    m.add(_T0, _T0 + timedelta(minutes=5), 6.0)                 # 0.5 kWh of 1.0
    now = _T0 + timedelta(minutes=5)
    assert m.allowed_import_w(charges, now) == pytest.approx(0.5 / (10 / 60) * 1000)
    # The plan accepted 6 kW for this slot: budget 1.5 kWh.
    assert m.allowed_import_w(charges, now, planned_kw=6.0) == pytest.approx(1.0 / (10 / 60) * 1000)
    # Near the end of the interval the rest is spread over at least 5 minutes.
    late = _T0 + timedelta(minutes=14)
    assert m.allowed_import_w(charges, late) == pytest.approx(0.5 / (5 / 60) * 1000)
    assert m.allowed_import_w(PowerCharges(), now) is None


def test_peak_from_history() -> None:
    now = datetime(2026, 11, 18, 10, 7, tzinfo=UTC)
    rows = [
        (datetime(2026, 11, 17, 8, 0, tzinfo=UTC), 3000.0),
        (datetime(2026, 11, 17, 8, 5, tzinfo=UTC), 9000.0),
        (datetime(2026, 11, 17, 8, 10, tzinfo=UTC), 6000.0),   # 6 kW average
        (datetime(2026, 11, 17, 9, 0, tzinfo=UTC), 12000.0),   # 4 kW (one bucket)
        (datetime(2026, 11, 18, 10, 0, tzinfo=UTC), 20000.0),  # running interval
    ]
    peak, at = peak_from_history(rows, now)
    assert peak == pytest.approx(6.0)
    assert at == datetime(2026, 11, 17, 8, 0, tzinfo=UTC).isoformat()


# ── Planner ───────────────────────────────────────────────────────────────────


async def _peak_import_kw(
    charges: PowerCharges | None,
    *,
    cheap_hours: int,
    month_peak_kw: float = 0.0,
    load_kw: float = 5.0,
) -> float:
    """Max planned 15-min import: a 5 kW house, one empty 10 kWh battery,
    ``cheap_hours`` of cheap power, then expensive power for the rest."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now.replace(minute=now.minute - now.minute % 15)
    battery = StorageConstraints(
        device_id="bat", capacity_kwh=10.0, max_charge_kw=3.0, max_discharge_kw=3.0,
        min_soc_pct=0.0, max_soc_pct=100.0,
    )
    steps = 8 * 4
    prices = [0.05 if t < cheap_hours * 4 else 0.50 for t in range(steps)]
    ctx = OptimizationContext(
        device_states={"bat": DeviceState(device_id="bat", soc_pct=0.0)},
        storage_constraints=[battery],
        forecasts={
            ForecastQuantity.PRICE: [
                ForecastPoint(timestamp=start + timedelta(minutes=15 * t), value=p)
                for t, p in enumerate(prices)
            ],
            ForecastQuantity.CONSUMPTION: [
                ForecastPoint(timestamp=start + timedelta(minutes=15 * t), value=load_kw)
                for t in range(steps)
            ],
        },
        horizon=timedelta(hours=8),
        power_charges=(
            PowerChargeContext(charges, month_peak_kw=month_peak_kw, interval_start=start)
            if charges is not None else None
        ),
    )
    plan = await MilpHigsOptimizer(step_minutes=15).optimize(ctx)
    # Skip the (possibly partial) first slot: its kW is over less time.
    return max(f.grid_import_kw for f in plan.flows[1:])


async def test_threshold_is_kept_when_the_cheap_window_is_long_enough() -> None:
    # 10 kWh in 6 cheap hours: 2 kW of charging keeps 5 + 2 = 7 kW.
    assert await _peak_import_kw(None, cheap_hours=6) <= 8.0 + 1e-3
    charges = PowerCharges((PowerThreshold(7.0, 0.11),))
    assert await _peak_import_kw(charges, cheap_hours=6) <= 7.0 + 1e-3


async def test_threshold_is_exceeded_when_avoiding_it_costs_more() -> None:
    # Only 2 cheap hours: charging at 3 kW (8 kW import) saves 0.45 €/kWh
    # on the later use, the excess costs 0.11 €/kWh.
    charges = PowerCharges((PowerThreshold(7.0, 0.11),))
    assert await _peak_import_kw(charges, cheap_hours=2) == pytest.approx(8.0, abs=1e-3)
    # A prohibitive excess price keeps it at the threshold.
    charges = PowerCharges((PowerThreshold(7.0, 5.0),))
    assert await _peak_import_kw(charges, cheap_hours=2) <= 7.0 + 1e-3


async def test_month_peak_is_not_raised_when_that_costs_more() -> None:
    expensive = PowerCharges(peaks=(MonthlyPeak(eur_per_kw_month=10.0),))
    assert await _peak_import_kw(expensive, cheap_hours=2, month_peak_kw=6.0) <= 6.0 + 1e-3


async def test_month_peak_is_raised_when_that_is_cheaper() -> None:
    cheap = PowerCharges(peaks=(MonthlyPeak(eur_per_kw_month=0.05),))
    assert await _peak_import_kw(cheap, cheap_hours=2, month_peak_kw=6.0) == pytest.approx(
        8.0, abs=1e-3)


async def test_power_up_to_the_month_peak_is_free() -> None:
    expensive = PowerCharges(peaks=(MonthlyPeak(eur_per_kw_month=10.0),))
    assert await _peak_import_kw(expensive, cheap_hours=2, month_peak_kw=8.0) == pytest.approx(
        8.0, abs=1e-3)


def test_interval_start() -> None:
    ts = datetime(2026, 11, 18, 10, 44, 59, tzinfo=UTC)
    assert interval_start(ts) == datetime(2026, 11, 18, 10, 30, tzinfo=UTC)


# ── Control loop ──────────────────────────────────────────────────────────────


def _battery_loop() -> ControlLoop:
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(StorageConstraints(
        device_id="bat", capacity_kwh=10.0, max_charge_kw=3.0, max_discharge_kw=3.0,
        min_soc_pct=10.0, max_soc_pct=95.0,
    )))
    loop.update_plan(EnergyPlan(created_at=_T0, intents=[ControlIntent(
        device_id="bat", timestep=_T0, power_kw=3.0, grid_allowed=True, reserved_kwh=0.75,
    )]))
    return loop


def _setpoint(loop: ControlLoop, house_w: float) -> float | None:
    live = LiveSituation(
        timestamp=_T0 + timedelta(minutes=1), grid_power_w=house_w, dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0)},
        current_price_eur_per_kwh=0.30, pv_opportunity_price_eur_per_kwh=0.08,
    )
    return {did: sp for did, sp, _m in loop.describe_setpoints(live)}["bat"]


def test_control_loop_curtails_to_the_power_charge_limit() -> None:
    assert _setpoint(_battery_loop(), 4000.0) == pytest.approx(3000.0)
    loop = _battery_loop()
    loop.set_power_charge_limit(5000.0)            # 4 kW house: 1 kW left
    assert _setpoint(loop, 4000.0) == pytest.approx(1000.0)
    assert set(loop.power_charge_curtailed) == {"bat"}
    loop.set_power_charge_limit(None)
    assert _setpoint(loop, 4000.0) == pytest.approx(3000.0)


# ── Config validation ─────────────────────────────────────────────────────────


def test_config_validation() -> None:
    ok = {"tariffs": {"grid": {
        "type": "flat_rate", "import_price_eur_per_kwh": 0.3, "default": True,
        "power_charges": [
            {"type": "power_threshold", "threshold_kw": 7.0, "excess_eur_per_kwh": 0.11},
            {"type": "monthly_peak", "eur_per_kw_month": 4.2, "minimum_kw": 2.5},
        ],
    }}}
    assert validate_sections(ok) == []
    bad = {"tariffs": {"grid": {
        "type": "flat_rate", "default": True,
        "power_charges": [{"type": "power_threshold", "threshold_kw": 7.0}],
    }}}
    assert any("excess_eur_per_kwh" in e for e in validate_sections(bad))


async def test_meter_survives_a_restart(tmp_path) -> None:
    from nornwatt.storage.sqlite import SqliteStorageBackend

    meter = PowerChargeMeter()
    meter.add(_T0, _T0 + timedelta(minutes=15), 6.0)
    db = SqliteStorageBackend(tmp_path / "h.db")
    await db.start()
    try:
        assert await db.load_power_charge_state() is None
        await db.save_power_charge_state(meter.to_dict())
    finally:
        await db.stop()
    db = SqliteStorageBackend(tmp_path / "h.db")
    await db.start()
    try:
        restored = PowerChargeMeter.from_dict(await db.load_power_charge_state())
    finally:
        await db.stop()
    assert restored == meter
    assert restored.month_peak_kw(_T0) == pytest.approx(6.0)

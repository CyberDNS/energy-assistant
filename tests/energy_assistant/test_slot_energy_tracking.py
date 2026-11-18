"""Charge intents deliver the slot's planned energy, not just its average power."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.core.control import ControlLoop, LiveSituation, StorageControlContributor
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import (
    ControlIntent,
    DeviceState,
    EnergyPlan,
    StorageConstraints,
)

_SLOT = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
_TICK_S = 60.0


def _loop(power_kw: float, *, max_charge_kw: float = 3.0, grid_allowed: bool = True) -> ControlLoop:
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(StorageConstraints(
        device_id="bat", capacity_kwh=10.0, max_charge_kw=max_charge_kw,
        max_discharge_kw=3.0, min_soc_pct=10.0, max_soc_pct=95.0,
    )))
    loop.update_plan(EnergyPlan(
        created_at=_SLOT - timedelta(seconds=30),
        intents=[ControlIntent(
            device_id="bat", timestep=_SLOT, power_kw=power_kw,
            grid_allowed=grid_allowed, reserved_kwh=power_kw * 0.25,
        )],
    ))
    return loop


def _tick(loop: ControlLoop, minute: float, measured_w: float, *, grid_w: float = 500.0,
          dt_s: float = _TICK_S) -> float:
    live = LiveSituation(
        timestamp=_SLOT + timedelta(minutes=minute),
        grid_power_w=grid_w + measured_w,  # house load + the battery's own draw
        dt_hours=dt_s / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=measured_w, soc_pct=50.0)},
        current_price_eur_per_kwh=0.30,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    (_did, setpoint_w, _mode), = loop.describe_setpoints(live)
    assert setpoint_w is not None
    return setpoint_w


def test_on_plan_delivery_keeps_planned_power() -> None:
    loop = _loop(1.0)
    for minute in range(0, 10):
        sp = _tick(loop, minute, 1000.0)
    assert sp == pytest.approx(1000.0)


def test_shortfall_is_made_up_over_the_rest_of_the_slot() -> None:
    """5 min at 500 W instead of 1 kW → 41.7 Wh behind, spread over the
    remaining 10 min → +250 W."""
    loop = _loop(1.0)
    _tick(loop, 0, 500.0)                 # first tick starts tracking
    for minute in range(1, 6):
        sp = _tick(loop, minute, 500.0)   # 5 ticks × 1 min at 500 W
    assert sp == pytest.approx(1000.0 + 500.0 * 5 / 10, rel=1e-3)


def test_over_delivery_lowers_the_target() -> None:
    """Battery absorbed extra surplus: it does not buy the full plan from the grid."""
    loop = _loop(1.0)
    _tick(loop, 0, 1500.0)
    for minute in range(1, 6):
        sp = _tick(loop, minute, 1500.0)
    assert sp == pytest.approx(1000.0 - 500.0 * 5 / 10, rel=1e-3)


def test_catch_up_is_clamped_to_device_max() -> None:
    loop = _loop(1.2, max_charge_kw=1.2)
    _tick(loop, 0, 0.0)
    for minute in range(1, 10):
        sp = _tick(loop, minute, 0.0)
    assert sp == pytest.approx(1200.0)


def test_status_previews_do_not_count_energy() -> None:
    """``dt_hours == 0`` (API status) must not advance the tracking."""
    loop = _loop(1.0)
    _tick(loop, 0, 0.0)
    for _ in range(20):
        sp = _tick(loop, 5, 0.0, dt_s=0.0)
    assert sp == pytest.approx(1000.0)


def test_new_plan_restarts_tracking() -> None:
    loop = _loop(1.0)
    _tick(loop, 0, 0.0)
    for minute in range(1, 6):
        _tick(loop, minute, 0.0)
    # Replan mid-slot: the new plan already knows the stored energy.
    loop.update_plan(EnergyPlan(
        created_at=_SLOT + timedelta(minutes=5, seconds=30),
        intents=[ControlIntent(device_id="bat", timestep=_SLOT, power_kw=1.0, reserved_kwh=0.16)],
    ))
    assert _tick(loop, 6, 0.0) == pytest.approx(1000.0)


def test_pv_only_intent_catches_up_when_surplus_returns() -> None:
    """No surplus for 5 min, then 1.3 kW: the PV-only battery claims its
    catch-up target (1 kW + 5/9 kW) from the pool, so it takes all 1.3 kW
    and the grid-capable battery charges fully from the grid (without the
    catch-up it would stop at 1 kW and leave 300 W of PV to the other)."""
    loop = ControlLoop(ledger=BatteryCostLedger())
    for did in ("bat", "ac"):
        loop.register_contributor(StorageControlContributor(StorageConstraints(
            device_id=did, capacity_kwh=10.0, max_charge_kw=3.0,
            max_discharge_kw=3.0, min_soc_pct=10.0, max_soc_pct=95.0,
        )))
    loop.update_plan(EnergyPlan(
        created_at=_SLOT - timedelta(seconds=30),
        intents=[
            ControlIntent(device_id="bat", timestep=_SLOT, power_kw=1.0,
                          grid_allowed=False, reserved_kwh=0.25),
            ControlIntent(device_id="ac", timestep=_SLOT, power_kw=1.0,
                          grid_allowed=True, reserved_kwh=0.25),
        ],
    ))

    def tick(minute: float, base_grid_w: float, dt_s: float = _TICK_S) -> dict[str, float | None]:
        measured = {"bat": 0.0, "ac": 1000.0}   # "ac" is on plan, "bat" falls behind
        live = LiveSituation(
            timestamp=_SLOT + timedelta(minutes=minute),
            grid_power_w=base_grid_w + sum(measured.values()),
            dt_hours=dt_s / 3600.0,
            device_states={
                did: DeviceState(device_id=did, power_w=w, soc_pct=50.0)
                for did, w in measured.items()
            },
            current_price_eur_per_kwh=0.30,
            pv_opportunity_price_eur_per_kwh=0.08,
        )
        return {did: sp for did, sp, _m in loop.describe_setpoints(live)}

    for minute in range(0, 6):
        assert tick(minute, 300.0)["bat"] == 0.0   # no surplus yet
    sp = tick(6, -1300.0, dt_s=0.0)
    assert sp["bat"] == pytest.approx(1300.0)
    assert sp["ac"] == pytest.approx(1000.0)

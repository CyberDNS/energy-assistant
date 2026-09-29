"""Tests for the missed-deadline catch-up gate.

A missed deadline is only caught up at full power if the car was plugged in
when it passed and has stayed plugged in since. Application tracks the
plug-in time (``_ev_plugged_since``) in ``_check_force_charge_reset``;
``resolve_active_goals`` applies the rule.

This replaced date-based dismissal on unplug and at startup, which kept
leaking cases:
- dismissal only fired if the goal was already overdue at the instant of
  the unplug, so a car unplugged while merely infeasible — or with no goal
  at all — resumed forcing full power on return;
- dismissal was keyed to the calendar day of the unplug, so a car unplugged
  the evening before (2026-09-29, schlumpf) had the next morning's deadline
  pass while it was away and was force-charged on its return that
  afternoon.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from energy_assistant.assets.ev import (
    ChargeCurvePoint,
    EvChargingAsset,
    EvDayOverride,
    EvWeeklyTarget,
)
from energy_assistant.assets.loader import resolve_active_goals
from energy_assistant.core.models import DeviceState
from energy_assistant.server import Application

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)  # 12:00 Europe/Berlin
WEEKLY_90_BY_0600 = {
    wd: EvWeeklyTarget(weekday=wd, enabled=True, target_soc_pct=90.0, target_by="06:00")
    for wd in range(1, 8)
}


def _asset(**kw) -> EvChargingAsset:
    params = dict(asset_id="ev1", device_id="wallbox", label="EV",
                  capacity_kwh=60.0, max_charge_kw=11.0, timezone="Europe/Berlin")
    params.update(kw)
    return EvChargingAsset(**params)


def _state(plugged: bool, soc: float = 40.0) -> DeviceState:
    return DeviceState(
        device_id="wallbox", power_w=0.0, soc_pct=soc, available=plugged,
        extra={"plugged": plugged},
    )


def _app(asset: EvChargingAsset | None = None) -> Application:
    app = Application()
    app._ev_assets = [asset or _asset()]
    app._ev_force_charge = {}
    app._ev_prev_plugged = {}
    app._ev_plugged_since = {}
    app._ev_overdue_dismissed = {}
    return app


async def _tick(app: Application, plugged: bool, at: datetime, soc: float = 40.0) -> None:
    await app._check_force_charge_reset({"wallbox": _state(plugged, soc)}, now=at)


def _goals(app: Application, at: datetime, soc: float, overrides=None):
    return resolve_active_goals(
        app._ev_assets, {"wallbox": _state(True, soc)},
        {"ev1": WEEKLY_90_BY_0600}, {"ev1": overrides or {}},
        now=at, overdue_dismissed=app._ev_overdue_dismissed,
        plugged_since=app._ev_plugged_since,
    )


# ---------------------------------------------------------------------------
# Plug tracking
# ---------------------------------------------------------------------------


async def test_plug_in_time_is_tracked_and_cleared_on_unplug() -> None:
    app = _app()
    await _tick(app, False, NOW)
    assert app._ev_plugged_since["ev1"] is None
    plug_in = NOW + timedelta(minutes=5)
    await _tick(app, True, plug_in)
    assert app._ev_plugged_since["ev1"] == plug_in
    await _tick(app, True, plug_in + timedelta(minutes=5))       # staying plugged
    assert app._ev_plugged_since["ev1"] == plug_in
    await _tick(app, False, plug_in + timedelta(minutes=10))
    assert app._ev_plugged_since["ev1"] is None


async def test_first_observation_after_restart_counts_as_fresh_plug_in() -> None:
    """How long the car was plugged before a restart is unknown, so a
    deadline that passed before the restart must not be caught up."""
    app = _app()
    await _tick(app, True, NOW)
    assert app._ev_plugged_since["ev1"] == NOW
    goals = _goals(app, NOW, soc=40.0)               # 06:00 today already passed
    assert len(goals) == 1 and not goals[0].overdue


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


async def test_car_plugged_through_the_deadline_is_still_caught_up() -> None:
    """The catch-up itself must keep working: plugged in overnight, deadline
    missed (e.g. unreachable), still plugged → keep forcing toward it."""
    app = _app()
    await _tick(app, True, datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc))  # evening before
    goals = _goals(app, NOW, soc=40.0)
    assert len(goals) == 1
    assert goals[0].overdue
    assert goals[0].target_by == datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)


async def test_unplugged_while_catching_up_then_back_follows_next_plan() -> None:
    app = _app()
    await _tick(app, True, datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc))
    assert _goals(app, NOW, soc=40.0)[0].overdue
    await _tick(app, False, NOW + timedelta(minutes=10))
    back = NOW + timedelta(hours=3)
    await _tick(app, True, back)
    goals = _goals(app, back, soc=35.0)
    assert not goals[0].overdue
    assert goals[0].target_by == datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)   # tomorrow


async def test_schlumpf_2026_09_29_unplugged_evening_before_deadline_passes_while_away() -> None:
    """Replay: unplugged on the 28th, away overnight while the 29th 06:00
    deadline passes, back at 16:42 on the 29th at 28% — must follow the
    next day's plan, not force-charge toward the deadline missed while away."""
    app = _app(_asset(capacity_kwh=52.0, charge_curve=[
        ChargeCurvePoint(90.0, 0.90), ChargeCurvePoint(100.0, 0.55)]))
    await _tick(app, True, datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc))
    await _tick(app, False, datetime(2026, 9, 28, 16, 30, tzinfo=timezone.utc))  # 18:30 local
    back = datetime(2026, 9, 29, 14, 42, tzinfo=timezone.utc)                    # 16:42 local
    await _tick(app, True, back, soc=28.0)
    goals = _goals(app, back, soc=28.0)
    assert len(goals) == 1
    assert not goals[0].overdue
    assert goals[0].target_by == datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)


async def test_override_unplugged_before_its_deadline_then_back_after_plans_tomorrow() -> None:
    """Replay of 2026-09-27: override 35% by 11:00, infeasible, unplugged at
    10:46 (deadline still ahead), back at 13:29 at 25%."""
    app = _app(_asset(capacity_kwh=77.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
                      charge_curve=[ChargeCurvePoint(90.0, 0.90), ChargeCurvePoint(100.0, 0.55)]))
    today = date(2026, 9, 27)
    overrides = {today: EvDayOverride(date=today, skip=False, target_soc_pct=35.0, target_by="11:00")}
    await _tick(app, True, datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc), soc=23.0)
    t_set = datetime(2026, 9, 27, 8, 7, tzinfo=timezone.utc)
    goals = _goals(app, t_set, soc=23.0, overrides=overrides)
    assert goals[0].infeasible and not goals[0].overdue

    await _tick(app, False, datetime(2026, 9, 27, 8, 46, tzinfo=timezone.utc), soc=30.0)
    back = datetime(2026, 9, 27, 11, 29, tzinfo=timezone.utc)
    await _tick(app, True, back, soc=25.0)
    goals = _goals(app, back, soc=25.0, overrides=overrides)
    assert not goals[0].overdue and not goals[0].infeasible
    assert goals[0].target_soc_pct == 90.0
    assert goals[0].target_by == datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)


async def test_away_car_is_not_shown_as_overdue() -> None:
    """While the car is unplugged its goal is still computed (for the UI and
    logs); it must point at the next target, not claim to force full power."""
    app = _app()
    await _tick(app, False, NOW)
    goals = _goals(app, NOW, soc=40.0)
    assert not goals[0].overdue

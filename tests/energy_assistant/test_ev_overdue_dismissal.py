"""Tests for Application._check_force_charge_reset's missed-deadline dismissal.

Regression 1: unplugging a car that was in the forced full-power catch-up
(overdue) state, then replugging it later the same day, used to resume
chasing the same missed deadline immediately — instead of following the
next scheduled plan, as if the car had never been unplugged at all.

Regression 2: dismissal only fired when the goal was already `overdue` at
the exact instant of the unplug. A car unplugged while merely `infeasible`
(deadline still ahead but unreachable at max power) — or with no goal at
all, e.g. already at target — would sail past its deadline while away with
nothing watching, then resume forcing full power the moment it reconnected,
because no dismissal was ever recorded. Dismissal is now unconditional on
any unplug: it only ever suppresses the overdue catch-up for a deadline
that has *already passed* by the time it's checked, so it's a no-op if the
deadline is still ahead on replug — safe to always record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from energy_assistant.assets.ev import EvChargingAsset, EvWeeklyTarget, build_goal_from_parts
from energy_assistant.assets.loader import asset_zoneinfo, resolve_active_goals
from energy_assistant.core.models import DeviceState
from energy_assistant.server import Application, _initial_overdue_dismissal

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)  # 12:00 Europe/Berlin


def _asset() -> EvChargingAsset:
    return EvChargingAsset(
        asset_id="ev1", device_id="wallbox", label="EV",
        capacity_kwh=60.0, max_charge_kw=11.0,
    )


def _overdue_goal(now: datetime) -> object:
    return build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=now - timedelta(hours=4),
        charge_curve=[], current_soc_pct=40.0, connected=True,
        now=now, overdue=True,
    )


def _plugged_state(plugged: bool, soc: float = 40.0) -> DeviceState:
    return DeviceState(
        device_id="wallbox", power_w=0.0, soc_pct=soc, available=True,
        extra={"plugged": plugged},
    )


def _app_with_overdue_goal() -> Application:
    app = Application()
    app._ev_assets = [_asset()]
    app._ev_force_charge = {}
    app._ev_prev_plugged = {}
    app._ev_overdue_dismissed = {}
    app._last_ev_goals = [_overdue_goal(NOW)]
    return app


async def test_unplug_while_overdue_records_dismissal() -> None:
    app = _app_with_overdue_goal()

    # Plugged in, then unplugged.
    await app._check_force_charge_reset({"wallbox": _plugged_state(True)}, now=NOW)
    await app._check_force_charge_reset({"wallbox": _plugged_state(False)}, now=NOW)

    assert app._ev_overdue_dismissed.get("ev1") == NOW.astimezone(ZoneInfo("Europe/Berlin")).date()


async def test_dismissal_happens_even_when_goal_is_only_infeasible_not_overdue() -> None:
    """The deadline is still ahead (merely infeasible, not yet overdue) at
    the moment of unplug — dismissal must still be recorded, because the
    deadline can quietly pass while the car is away with nobody watching."""
    app = _app_with_overdue_goal()
    app._last_ev_goals = [
        build_goal_from_parts(
            asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
            max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
            target_soc_pct=90.0, target_by=NOW + timedelta(minutes=30),
            charge_curve=[], current_soc_pct=10.0, connected=True, now=NOW,
        )
    ]
    assert app._last_ev_goals[0].infeasible
    assert not app._last_ev_goals[0].overdue

    await app._check_force_charge_reset({"wallbox": _plugged_state(True)}, now=NOW)
    await app._check_force_charge_reset({"wallbox": _plugged_state(False)}, now=NOW)

    assert app._ev_overdue_dismissed.get("ev1") == NOW.astimezone(ZoneInfo("Europe/Berlin")).date()


async def test_dismissal_happens_even_with_no_goal_at_all() -> None:
    """No goal exists at all (e.g. target already met) — dismissal must
    still be recorded on unplug, since a later drop in SoC (the car being
    driven) after the deadline passes must not resurrect it as overdue."""
    app = _app_with_overdue_goal()
    app._last_ev_goals = []

    await app._check_force_charge_reset({"wallbox": _plugged_state(True)}, now=NOW)
    await app._check_force_charge_reset({"wallbox": _plugged_state(False)}, now=NOW)

    assert app._ev_overdue_dismissed.get("ev1") == NOW.astimezone(ZoneInfo("Europe/Berlin")).date()


async def test_dismissal_blocks_overdue_on_replug_same_day() -> None:
    """End-to-end: after the unplug dismisses today's missed target,
    resolve_active_goals must not return it as overdue again on replug."""
    app = _app_with_overdue_goal()
    await app._check_force_charge_reset({"wallbox": _plugged_state(True)}, now=NOW)
    await app._check_force_charge_reset({"wallbox": _plugged_state(False)}, now=NOW)
    assert app._ev_overdue_dismissed  # sanity: dismissal was recorded

    weekly = {
        wd: EvWeeklyTarget(weekday=wd, enabled=True, target_soc_pct=90.0, target_by="06:00")
        for wd in range(1, 8)
    }
    goals = resolve_active_goals(
        app._ev_assets,
        {"wallbox": _plugged_state(True, soc=40.0)},  # replugged
        {"ev1": weekly},
        {},
        now=NOW,
        overdue_dismissed=app._ev_overdue_dismissed,
    )
    assert len(goals) == 1
    assert not goals[0].overdue


# ---------------------------------------------------------------------------
# Startup: don't resume overdue catch-up after a restart
# ---------------------------------------------------------------------------


def test_initial_overdue_dismissal_covers_every_asset_today() -> None:
    asset_a = _asset()
    asset_b = EvChargingAsset(
        asset_id="ev2", device_id="wallbox2", label="EV2",
        capacity_kwh=40.0, max_charge_kw=7.4,
    )
    dismissed = _initial_overdue_dismissal([asset_a, asset_b], NOW)
    expected_day = NOW.astimezone(asset_zoneinfo(asset_a)).date()
    assert dismissed == {"ev1": expected_day, "ev2": expected_day}


async def test_startup_dismissal_prevents_overdue_on_first_cycle_after_restart() -> None:
    """A car that's already overdue for today's deadline when the container
    comes back up must NOT immediately start forcing full power — it should
    behave as if it had just been unplugged and follow the next plan."""
    asset = _asset()
    dismissed = _initial_overdue_dismissal([asset], NOW)

    weekly = {
        wd: EvWeeklyTarget(weekday=wd, enabled=True, target_soc_pct=90.0, target_by="06:00")
        for wd in range(1, 8)
    }
    goals = resolve_active_goals(
        [asset],
        {"wallbox": _plugged_state(True, soc=40.0)},
        {"ev1": weekly},
        {},
        now=NOW,
        overdue_dismissed=dismissed,
    )
    assert len(goals) == 1
    assert not goals[0].overdue


async def test_real_world_override_unplug_then_return_after_deadline_plans_tomorrow() -> None:
    """Reproduces the 2026-09-27 log: day override 35% by 11:00 local, goal
    goes infeasible, car is unplugged at 10:46 local (deadline still ahead,
    so NOT overdue yet), the deadline passes while away, car returns at
    13:29 local well below 35%. It must plan for tomorrow's weekly target
    instead of force-charging toward today's already-blown override."""
    from datetime import date
    from energy_assistant.assets.ev import EvDayOverride

    today = date(2026, 9, 27)
    weekly = {
        wd: EvWeeklyTarget(weekday=wd, enabled=True, target_soc_pct=90.0, target_by="06:00")
        for wd in range(1, 8)
    }
    overrides = {today: EvDayOverride(date=today, skip=False, target_soc_pct=35.0, target_by="11:00")}

    from energy_assistant.assets.ev import ChargeCurvePoint

    app = Application()
    # banzert's real parameters from the deployed config
    app._ev_assets = [EvChargingAsset(
        asset_id="ev1", device_id="wallbox", label="Banzert",
        capacity_kwh=77.0, max_charge_kw=11.0, min_charge_kw=4.14,
        charge_limit_soc_pct=90.0, timezone="Europe/Berlin",
        charge_curve=[ChargeCurvePoint(90.0, 0.90), ChargeCurvePoint(100.0, 0.55)],
    )]
    app._ev_force_charge = {}
    app._ev_prev_plugged = {}
    app._ev_overdue_dismissed = {}

    # 10:07 local: override set, goal is infeasible (not overdue).
    t_set = datetime(2026, 9, 27, 8, 7, tzinfo=timezone.utc)
    goals = resolve_active_goals(
        app._ev_assets, {"wallbox": _plugged_state(True, soc=23.0)},
        {"ev1": weekly}, {"ev1": overrides},
        now=t_set, overdue_dismissed=app._ev_overdue_dismissed,
    )
    app._last_ev_goals = goals
    assert goals[0].infeasible and not goals[0].overdue

    # 10:46 local: unplugged while still merely infeasible.
    t_unplug = datetime(2026, 9, 27, 8, 46, tzinfo=timezone.utc)
    await app._check_force_charge_reset({"wallbox": _plugged_state(True, soc=30.0)}, now=t_unplug)
    await app._check_force_charge_reset({"wallbox": _plugged_state(False, soc=30.0)}, now=t_unplug)

    # 13:29 local: back home, well below 35%, deadline long passed.
    t_back = datetime(2026, 9, 27, 11, 29, tzinfo=timezone.utc)
    goals = resolve_active_goals(
        app._ev_assets, {"wallbox": _plugged_state(True, soc=25.0)},
        {"ev1": weekly}, {"ev1": overrides},
        now=t_back, overdue_dismissed=app._ev_overdue_dismissed,
    )
    assert len(goals) == 1
    g = goals[0]
    assert not g.overdue
    assert not g.infeasible
    assert g.target_soc_pct == 90.0
    assert g.target_by == datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)  # tomorrow 06:00 local

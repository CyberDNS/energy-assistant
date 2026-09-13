"""Tests for DB-backed EV charge plans: weekly plan + dated overrides
resolution, the upcoming-days strip, force-charge control, and the SQLite
persistence round-trips."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from energy_assistant.assets.ev import (
    EvChargerContributor,
    EvChargingAsset,
    EvDayOverride,
    EvWeeklyTarget,
    build_goal_from_parts,
)
from energy_assistant.assets.loader import resolve_active_goals, upcoming_days
from energy_assistant.core.control import LiveSituation
from energy_assistant.core.models import DeviceState
from energy_assistant.storage.sqlite import SqliteStorageBackend

TZ = ZoneInfo("Europe/Berlin")

# Wed 2026-07-15 12:00 Berlin (= 10:00 UTC)
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 15)          # ISO weekday 3 (Wed)
TOMORROW = date(2026, 7, 16)


def _asset(asset_id: str = "ev1", device_id: str = "wallbox") -> EvChargingAsset:
    return EvChargingAsset(
        asset_id=asset_id,
        device_id=device_id,
        label="EV",
        capacity_kwh=60.0,
        max_charge_kw=11.0,
        charge_limit_soc_pct=90.0,
        timezone="Europe/Berlin",
    )


def _weekly_all_days(soc: float = 80.0, hhmm: str = "06:00") -> dict[int, EvWeeklyTarget]:
    return {
        wd: EvWeeklyTarget(weekday=wd, enabled=True, target_soc_pct=soc, target_by=hhmm)
        for wd in range(1, 8)
    }


def _weekly_today_already_met(soc: float = 80.0, hhmm: str = "06:00") -> dict[int, EvWeeklyTarget]:
    """Weekly plan where *today's* (Wed, weekday 3) target is already below
    the default 40% test SoC, so it doesn't go overdue — used by tests that
    exercise the forward-walk to a later day, independent of the overdue
    catch-up behavior (covered separately, see test_missed_deadline_*)."""
    weekly = _weekly_all_days(soc=soc, hhmm=hhmm)
    weekly[3] = EvWeeklyTarget(weekday=3, enabled=True, target_soc_pct=30.0, target_by=hhmm)
    return weekly


def _state(soc: float = 40.0, available: bool = True) -> DeviceState:
    return DeviceState(device_id="wallbox", power_w=0.0, soc_pct=soc, available=available)


def _resolve(weekly, overrides, *, soc: float = 40.0, now: datetime = NOW):
    asset = _asset()
    goals = resolve_active_goals(
        [asset],
        {"wallbox": _state(soc)},
        {"ev1": weekly},
        {"ev1": overrides},
        now=now,
    )
    # Filter out the pv_only no-plan fallback goal
    return [g for g in goals if not g.pv_only]


# ---------------------------------------------------------------------------
# Weekly plan resolution
# ---------------------------------------------------------------------------


def test_weekly_plan_targets_next_morning() -> None:
    # Today's 30% target is already met by the 40% test SoC, so it isn't
    # overdue and the walk moves on to tomorrow's 80% @ 06:00 Berlin.
    goals = _resolve(_weekly_today_already_met(), {})
    assert len(goals) == 1
    g = goals[0]
    assert g.target_soc_pct == 80.0
    expected = datetime(2026, 7, 16, 6, 0, tzinfo=TZ).astimezone(timezone.utc)
    assert g.target_by == expected
    assert not g.overdue


def test_disabled_weekday_is_stepped_over() -> None:
    weekly = _weekly_today_already_met()
    weekly[4] = EvWeeklyTarget(weekday=4, enabled=False, target_soc_pct=80.0, target_by="06:00")
    goals = _resolve(weekly, {})
    # Thursday (weekday 4) disabled → Friday 06:00
    expected = datetime(2026, 7, 17, 6, 0, tzinfo=TZ).astimezone(timezone.utc)
    assert goals[0].target_by == expected


def test_missed_deadline_keeps_forcing_todays_target() -> None:
    """When today's deadline has passed and SoC hasn't caught up, the goal
    should stick to *today's* target (overdue=True) instead of silently
    rolling over to tomorrow — this is what drives the full-power catch-up
    charge (see EvChargerContributor / build_goal_from_parts)."""
    goals = _resolve(_weekly_all_days(), {}, soc=40.0)
    assert len(goals) == 1
    g = goals[0]
    assert g.overdue
    assert g.target_soc_pct == 80.0
    expected_today = datetime(2026, 7, 15, 6, 0, tzinfo=TZ).astimezone(timezone.utc)
    assert g.target_by == expected_today
    # phase1/phase2 merged into one immediate full-power block
    assert g.phase1_required_kwh == 0.0
    assert g.phase2_start_time <= NOW


def test_missed_deadline_stops_once_target_reached() -> None:
    goals = _resolve(_weekly_all_days(), {}, soc=80.0)
    assert goals == []


def test_empty_plan_yields_pv_only_goal_when_connected() -> None:
    asset = _asset()
    goals = resolve_active_goals([asset], {"wallbox": _state()}, {}, {}, now=NOW)
    assert len(goals) == 1
    assert goals[0].pv_only


def test_future_deadline_today_is_used() -> None:
    goals = _resolve(_weekly_all_days(hhmm="18:00"), {})
    expected = datetime(2026, 7, 15, 18, 0, tzinfo=TZ).astimezone(timezone.utc)
    assert goals[0].target_by == expected


# ---------------------------------------------------------------------------
# Dated overrides
# ---------------------------------------------------------------------------


def test_skip_override_moves_to_next_day() -> None:
    weekly = _weekly_all_days(hhmm="18:00")
    overrides = {TODAY: EvDayOverride(date=TODAY, skip=True)}
    goals = _resolve(weekly, overrides)
    expected = datetime(2026, 7, 16, 18, 0, tzinfo=TZ).astimezone(timezone.utc)
    assert goals[0].target_by == expected


def test_override_replaces_soc_and_time() -> None:
    weekly = _weekly_today_already_met(hhmm="06:00")
    overrides = {
        TOMORROW: EvDayOverride(
            date=TOMORROW, skip=False, target_soc_pct=100.0, target_by="09:30"
        )
    }
    goals = _resolve(weekly, overrides)
    assert goals[0].target_soc_pct == 100.0
    expected = datetime(2026, 7, 16, 9, 30, tzinfo=TZ).astimezone(timezone.utc)
    assert goals[0].target_by == expected


def test_override_partial_falls_back_to_weekly_time() -> None:
    weekly = _weekly_today_already_met(soc=80.0, hhmm="06:00")
    overrides = {
        TOMORROW: EvDayOverride(date=TOMORROW, skip=False, target_soc_pct=55.0)
    }
    goals = _resolve(weekly, overrides)
    assert goals[0].target_soc_pct == 55.0
    expected = datetime(2026, 7, 16, 6, 0, tzinfo=TZ).astimezone(timezone.utc)
    assert goals[0].target_by == expected


def test_override_on_disabled_weekday_creates_target() -> None:
    weekly = {
        wd: EvWeeklyTarget(weekday=wd, enabled=False, target_soc_pct=80.0, target_by="06:00")
        for wd in range(1, 8)
    }
    overrides = {
        TOMORROW: EvDayOverride(
            date=TOMORROW, skip=False, target_soc_pct=70.0, target_by="07:00"
        )
    }
    goals = _resolve(weekly, overrides)
    assert goals[0].target_soc_pct == 70.0


def test_soc_already_at_target_yields_no_goal() -> None:
    goals = _resolve(_weekly_all_days(soc=80.0), {}, soc=85.0)
    assert goals == []


# ---------------------------------------------------------------------------
# Feasibility (build_goal_from_parts)
# ---------------------------------------------------------------------------


def test_feasible_goal_keeps_two_phase_split() -> None:
    # Plenty of time: 10 % → 90 % at 11 kW easily fits in 12 hours.
    goal = build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=NOW + timedelta(hours=12),
        charge_curve=[], current_soc_pct=10.0, connected=True,
        now=NOW,
    )
    assert not goal.infeasible
    assert goal.phase1_required_kwh > 0.0
    assert goal.phase2_start_time > NOW


def test_infeasible_goal_forces_immediate_full_power() -> None:
    # 10 % → 90 % needs ~44 kWh at 11 kW (~4 h), but only 1 h is left.
    goal = build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=NOW + timedelta(hours=1),
        charge_curve=[], current_soc_pct=10.0, connected=True,
        now=NOW,
    )
    assert goal.infeasible
    assert goal.phase1_required_kwh == 0.0
    assert goal.phase2_required_kwh > 0.0
    assert goal.phase2_start_time == NOW


def test_overdue_goal_forces_immediate_full_power_regardless_of_time_left() -> None:
    # target_by already passed; overdue=True should merge phases even
    # though feasibility wasn't (and can't sensibly be) checked.
    goal = build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=NOW - timedelta(hours=2),
        charge_curve=[], current_soc_pct=10.0, connected=True,
        now=NOW, overdue=True,
    )
    assert goal.overdue
    assert goal.phase1_required_kwh == 0.0
    assert goal.phase2_start_time == NOW


def test_infeasible_goal_still_stops_control_at_target() -> None:
    """The contributor's existing 'target met → Stop' check must still win
    once the forced full-power catch-up reaches the target."""
    contrib = EvChargerContributor(_asset())
    goal = build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=NOW + timedelta(minutes=30),
        charge_curve=[], current_soc_pct=10.0, connected=True,
        now=NOW,
    )
    assert goal.infeasible
    contrib.update_goal(goal)

    live_mid = _live({"wallbox": _state(soc=50.0)})
    assert contrib.desired_setpoint_w(None, live_mid) == 11_000.0

    live_done = _live({"wallbox": _state(soc=90.0)})
    assert contrib.desired_setpoint_w(None, live_done) == 0.0


# ---------------------------------------------------------------------------
# upcoming_days strip
# ---------------------------------------------------------------------------


def test_upcoming_days_sources_and_passed_flag() -> None:
    weekly = _weekly_all_days(hhmm="06:00")
    overrides = {
        TODAY: EvDayOverride(date=TODAY, skip=True),
        TOMORROW: EvDayOverride(
            date=TOMORROW, skip=False, target_soc_pct=100.0, target_by="09:00"
        ),
    }
    days = upcoming_days(_asset(), weekly, overrides, now=NOW)
    assert len(days) == 7
    assert days[0]["date"] == TODAY.isoformat()
    # Today skipped — and it stays visibly skipped for the whole day
    assert days[0]["source"] == "skip"
    assert days[1]["source"] == "override"
    assert days[1]["target_soc_pct"] == 100.0
    assert days[2]["source"] == "weekly"
    # 06:00 deadlines from day 3 onwards are in the future
    assert not days[2]["passed"]


def test_upcoming_days_marks_passed_deadline_today() -> None:
    # Weekly deadline 06:00 — now is 12:00 local, so today's entry is passed
    days = upcoming_days(_asset(), _weekly_all_days(hhmm="06:00"), {}, now=NOW)
    assert days[0]["source"] == "weekly"
    assert days[0]["passed"] is True


# ---------------------------------------------------------------------------
# Force charge — contributor behaviour
# ---------------------------------------------------------------------------


def _live(device_states: dict[str, DeviceState]) -> LiveSituation:
    return LiveSituation(
        timestamp=NOW,
        grid_power_w=0.0,
        dt_hours=30 / 3600,
        device_states=device_states,
    )


def test_force_charge_overrides_plan_with_full_speed() -> None:
    contrib = EvChargerContributor(_asset())
    # Active goal that would normally map to PV/Stop
    goal = build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=NOW + timedelta(hours=12),
        charge_curve=[], current_soc_pct=40.0, connected=True,
    )
    contrib.update_goal(goal)
    contrib.set_force_charge(100.0)

    live = _live({"wallbox": _state(soc=40.0)})
    assert contrib.desired_setpoint_w(None, live) == 11_000.0


def test_force_charge_stops_at_target() -> None:
    contrib = EvChargerContributor(_asset())
    contrib.set_force_charge(80.0)
    live = _live({"wallbox": _state(soc=81.0)})
    assert contrib.desired_setpoint_w(None, live) == 0.0


def test_force_charge_cleared_returns_to_normal() -> None:
    contrib = EvChargerContributor(_asset())
    contrib.set_force_charge(100.0)
    contrib.set_force_charge(None)
    live = _live({"wallbox": _state(soc=40.0)})
    # No goal → opportunistic PV sentinel
    setpoint = contrib.desired_setpoint_w(None, live)
    assert setpoint is not None and 0.0 < setpoint <= 500.0


def test_force_charge_disabled_chargepoint_still_hands_off() -> None:
    contrib = EvChargerContributor(_asset())
    contrib.set_force_charge(100.0)
    contrib.set_disabled(True)
    live = _live({"wallbox": _state(soc=40.0)})
    assert contrib.desired_setpoint_w(None, live) is None


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weekly_plan_roundtrip(tmp_path: Path) -> None:
    storage = SqliteStorageBackend(tmp_path / "test.db")
    await storage.start()
    try:
        entries = [
            EvWeeklyTarget(weekday=wd, enabled=wd <= 5, target_soc_pct=80.0, target_by="06:00")
            for wd in range(1, 8)
        ]
        await storage.set_ev_weekly_plan("ev1", entries)
        loaded = await storage.load_all_ev_weekly_plans()
        assert set(loaded["ev1"]) == set(range(1, 8))
        assert loaded["ev1"][6].enabled is False
        assert loaded["ev1"][1].target_by == "06:00"

        # Replacing the plan removes stale rows
        await storage.set_ev_weekly_plan("ev1", entries[:2])
        loaded = await storage.load_all_ev_weekly_plans()
        assert set(loaded["ev1"]) == {1, 2}
    finally:
        await storage.stop()


@pytest.mark.asyncio
async def test_day_override_roundtrip_and_purge(tmp_path: Path) -> None:
    storage = SqliteStorageBackend(tmp_path / "test.db")
    await storage.start()
    try:
        yesterday = TODAY - timedelta(days=1)
        await storage.set_ev_day_override("ev1", EvDayOverride(date=yesterday, skip=True))
        await storage.set_ev_day_override(
            "ev1",
            EvDayOverride(date=TODAY, skip=False, target_soc_pct=100.0, target_by="09:00"),
        )
        loaded = await storage.load_all_ev_day_overrides()
        assert set(loaded["ev1"]) == {yesterday, TODAY}
        assert loaded["ev1"][TODAY].target_soc_pct == 100.0
        assert loaded["ev1"][yesterday].skip is True

        # Purge drops only dates strictly before today
        await storage.purge_ev_day_overrides_before("ev1", TODAY)
        loaded = await storage.load_all_ev_day_overrides()
        assert set(loaded["ev1"]) == {TODAY}

        await storage.clear_ev_day_override("ev1", TODAY)
        loaded = await storage.load_all_ev_day_overrides()
        assert loaded.get("ev1", {}) == {}
    finally:
        await storage.stop()


@pytest.mark.asyncio
async def test_force_charge_roundtrip(tmp_path: Path) -> None:
    storage = SqliteStorageBackend(tmp_path / "test.db")
    await storage.start()
    try:
        await storage.set_ev_force_charge("ev1", 95.0)
        assert await storage.load_all_ev_force_charge() == {"ev1": 95.0}
        await storage.clear_ev_force_charge("ev1")
        assert await storage.load_all_ev_force_charge() == {}
    finally:
        await storage.stop()

"""Tests for Application._check_force_charge_reset's missed-deadline dismissal.

Regression: unplugging a car that was in the forced full-power catch-up
(overdue) state, then replugging it later the same day, used to resume
chasing the same missed deadline immediately — instead of following the
next scheduled plan, as if the car had never been unplugged at all.
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
    await app._check_force_charge_reset({"wallbox": _plugged_state(True)})
    await app._check_force_charge_reset({"wallbox": _plugged_state(False)})

    assert app._ev_overdue_dismissed.get("ev1") == _overdue_goal(NOW).target_by.astimezone(
        ZoneInfo("Europe/Berlin")
    ).date()


async def test_no_dismissal_when_goal_is_not_overdue() -> None:
    app = _app_with_overdue_goal()
    # Feasible (non-overdue) goal — a normal unplug shouldn't touch it.
    app._last_ev_goals = [
        build_goal_from_parts(
            asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
            max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
            target_soc_pct=90.0, target_by=NOW + timedelta(hours=12),
            charge_curve=[], current_soc_pct=40.0, connected=True, now=NOW,
        )
    ]

    await app._check_force_charge_reset({"wallbox": _plugged_state(True)})
    await app._check_force_charge_reset({"wallbox": _plugged_state(False)})

    assert app._ev_overdue_dismissed == {}


async def test_dismissal_blocks_overdue_on_replug_same_day() -> None:
    """End-to-end: after the unplug dismisses today's missed target,
    resolve_active_goals must not return it as overdue again on replug."""
    app = _app_with_overdue_goal()
    await app._check_force_charge_reset({"wallbox": _plugged_state(True)})
    await app._check_force_charge_reset({"wallbox": _plugged_state(False)})
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

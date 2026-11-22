"""Parse the ``assets:`` config section and compute active ``EvChargingGoal`` objects.

Called by the planning loop on every optimization cycle so that the goals
always reflect the current SoC, the DB-backed weekly plan, and any dated
overrides/skips set in the UI.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.device_model import DeviceCategory, DeviceSpec
from ..core.models import DeviceState
from .ev import (
    _FEASIBILITY_MARGIN,
    ChargeCurvePoint,
    EvChargingAsset,
    EvChargingGoal,
    EvDayOverride,
    EvWeeklyTarget,
    build_goal_from_parts,
)

_log = logging.getLogger(__name__)

_DEFAULT_MAX_CHARGE_KW = 11.0
_DEFAULT_MIN_CHARGE_KW = 1.38  # 6 A × 230 V single phase
_GUEST_CAPACITY_KWH = 50.0

# Sentinel: plug tracking unavailable — don't gate the overdue catch-up on it.
_ALWAYS = object()


def vehicle_assets(devices: Mapping[str, DeviceSpec]) -> dict[str, EvChargingAsset]:
    """One ``EvChargingAsset`` per ``vehicle`` device, not at any charger yet.

    ``asset_id`` is the vehicle id; ``device_id`` is empty until a charging
    session puts the vehicle at a charger (``at_charger``).  The charging
    power limits come from the charger then.
    """
    out: dict[str, EvChargingAsset] = {}
    for spec in devices.values():
        if spec.category is not DeviceCategory.VEHICLE:
            continue
        p = spec.params
        curve = [
            ChargeCurvePoint(soc_pct=float(pt["soc_pct"]), efficiency=float(pt["efficiency"]))
            for pt in p.get("charge_curve") or []
        ] or [ChargeCurvePoint(80.0, 1.0), ChargeCurvePoint(100.0, 0.55)]
        out[spec.id] = EvChargingAsset(
            asset_id=spec.id,
            device_id="",
            label=spec.display_name,
            capacity_kwh=float(p["capacity_kwh"]),
            max_charge_kw=float(p.get("max_charge_kw", _DEFAULT_MAX_CHARGE_KW)),
            min_charge_kw=_DEFAULT_MIN_CHARGE_KW,
            charge_limit_soc_pct=float(p.get("charge_limit_soc_pct", 100.0)),
            charge_curve=curve,
            timezone=str(p.get("timezone", "Europe/Berlin")),
        )
    return out


def charger_limits(charger: DeviceSpec) -> tuple[float, float]:
    """``(min_power_kw, max_power_kw)`` of a charger."""
    p = charger.params
    return (
        float(p.get("min_power_kw", _DEFAULT_MIN_CHARGE_KW)),
        float(p.get("max_power_kw", _DEFAULT_MAX_CHARGE_KW)),
    )


def at_charger(
    vehicle: EvChargingAsset, charger: DeviceSpec, vehicle_max_kw: float | None = None
) -> EvChargingAsset:
    """*vehicle* charging at *charger*: its commands go to the charger, and
    the power limits are the charger's (capped by the vehicle's own
    ``max_charge_kw``, if set)."""
    min_kw, max_kw = charger_limits(charger)
    if vehicle_max_kw is not None:
        max_kw = min(max_kw, vehicle_max_kw)
    return replace(vehicle, device_id=charger.id, min_charge_kw=min_kw, max_charge_kw=max_kw)


def guest_asset(charger: DeviceSpec) -> EvChargingAsset:
    """Placeholder for an unknown car at *charger* (never planned)."""
    min_kw, max_kw = charger_limits(charger)
    return EvChargingAsset(
        asset_id=f"guest@{charger.id}", device_id=charger.id, label="Guest",
        capacity_kwh=_GUEST_CAPACITY_KWH, max_charge_kw=max_kw, min_charge_kw=min_kw,
    )


def resolve_active_goals(
    assets: list[EvChargingAsset],
    device_states: dict[str, DeviceState],
    weekly_plans: dict[str, dict[int, EvWeeklyTarget]],
    day_overrides: dict[str, dict[date, EvDayOverride]],
    now: datetime | None = None,
    overdue_dismissed: dict[str, date] | None = None,
    feasibility_margin: float = _FEASIBILITY_MARGIN,
    plugged_since: dict[str, datetime | None] | None = None,
) -> list[EvChargingGoal]:
    """Compute an ``EvChargingGoal`` for every asset that has an active target.

    Parameters
    ----------
    assets:
        Parsed asset configs.
    device_states:
        Latest state per device (from the registry).
    weekly_plans:
        DB-backed weekly plan per asset: asset_id → {iso_weekday → target}.
    day_overrides:
        Dated overrides/skips per asset: asset_id → {local_date → override}.
    now:
        Override "now" for testing.  Defaults to ``datetime.now(UTC)``.
    overdue_dismissed:
        asset_id → local date whose target was already met. Written here
        (mutated in place) the moment today's target is reached, so SoC
        dropping afterwards — the car being driven — doesn't resurrect that
        deadline as overdue.
    feasibility_margin:
        Safety buffer passed to ``build_goal_from_parts`` — see there.
        Overridable via ``controller.ev_feasibility_margin`` in config.yaml.
    plugged_since:
        asset_id → when the car was plugged in (None = unplugged / not yet
        seen), tracked by the charging sessions (``Application._on_session_changes``). A missed
        deadline is only caught up at full power if the car was plugged in
        when it passed and has stayed plugged in since: a deadline that
        passed while the car was away — unplugged the evening before, driven
        off before the deadline, or during a container restart — is left
        behind, and the car follows the next scheduled target on replug.
        ``None`` (the argument itself) disables the gate, e.g. in tests.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # NOTE: `or {}` would silently swap out an empty-but-real dict for a new
    # throwaway one, breaking the in-place mutation below (dismissing
    # today's target) exactly when it matters most — a fresh dict early on.
    if overdue_dismissed is None:
        overdue_dismissed = {}

    goals: list[EvChargingGoal] = []
    for asset in assets:
        # The charger's state (with the vehicle's SoC laid over it) while the
        # vehicle is at a charger; away, only the vehicle's own SoC is known.
        state = device_states.get(asset.device_id) if asset.device_id else None
        connected = state is not None and state.available
        soc_state = state if state is not None else device_states.get(asset.asset_id)
        current_soc = (soc_state.soc_pct or 0.0) if soc_state is not None else 0.0

        # If today's own target is already met, dismiss today for the
        # overdue catch-up right now — regardless of whether the deadline
        # has passed yet, and regardless of what _resolve_target ends up
        # returning (once satisfied, it may already have rolled over to a
        # later day's target, which must not be dismissed instead). This
        # matters because a car can meet its target early (e.g. finishes
        # charging before the deadline), then get driven — dropping SoC
        # back below that same target — without ever going "overdue" in
        # the first place, so the unplug-triggered dismissal in
        # Application._check_force_charge_reset never has an overdue goal
        # to act on. Without this, coming home later the same day resumes
        # a forced full-power charge toward a deadline that was already met.
        tz = asset_zoneinfo(asset)
        today_local = now.astimezone(tz).date()
        today_target = effective_day_target(
            weekly_plans.get(asset.asset_id, {}), day_overrides.get(asset.asset_id, {}), today_local,
        )
        if today_target is not None:
            today_soc, _today_hhmm, _today_source = today_target
            if current_soc >= today_soc:
                overdue_dismissed[asset.asset_id] = today_local

        target_info = _resolve_target(
            asset,
            weekly_plans.get(asset.asset_id, {}),
            day_overrides.get(asset.asset_id, {}),
            now,
            current_soc,
            overdue_dismissed.get(asset.asset_id),
            catch_up_if_plugged_since=(
                _ALWAYS if plugged_since is None else plugged_since.get(asset.asset_id)
            ),
        )
        if target_info is None:
            # No schedule: include connected EV as PV-only absorber so the plan
            # shows estimated surplus charging with real kW values.
            if connected:
                goals.append(build_goal_from_parts(
                    asset_id=asset.asset_id,
                    device_id=asset.device_id,
                    capacity_kwh=asset.capacity_kwh,
                    max_charge_kw=asset.max_charge_kw,
                    min_charge_kw=asset.min_charge_kw,
                    charge_limit_soc_pct=asset.charge_limit_soc_pct,
                    target_soc_pct=asset.charge_limit_soc_pct,
                    target_by=now + timedelta(hours=168),  # far future — no deadline
                    charge_curve=asset.charge_curve,
                    current_soc_pct=current_soc,
                    connected=connected,
                    pv_only=True,
                ))
            continue

        target_soc, target_by, overdue = target_info

        # Skip if already at target
        if current_soc >= target_soc:
            _log.debug("Asset %r: SoC %.0f%% ≥ target %.0f%% — no goal needed",
                       asset.asset_id, current_soc, target_soc)
            continue

        goal = build_goal_from_parts(
            asset_id=asset.asset_id,
            device_id=asset.device_id,
            capacity_kwh=asset.capacity_kwh,
            max_charge_kw=asset.max_charge_kw,
            min_charge_kw=asset.min_charge_kw,
            charge_limit_soc_pct=asset.charge_limit_soc_pct,
            target_soc_pct=target_soc,
            target_by=target_by,
            charge_curve=asset.charge_curve,
            current_soc_pct=current_soc,
            connected=connected,
            now=now,
            overdue=overdue,
            feasibility_margin=feasibility_margin,
        )
        if goal.overdue:
            _log.warning(
                "Asset %r: missed target_by %s (still %.0f%% of %.0f%%) — "
                "forcing full power until reached or unplugged",
                asset.asset_id, target_by.isoformat(), current_soc, target_soc,
            )
        elif goal.infeasible:
            _log.warning(
                "Asset %r: target of %.0f%% by %s is no longer reachable at "
                "max power from %.0f%% — forcing full power now",
                asset.asset_id, target_soc, target_by.isoformat(), current_soc,
            )
        goals.append(goal)

    return goals


def asset_zoneinfo(asset: EvChargingAsset) -> ZoneInfo:
    """The asset's local timezone (falls back to UTC on bad config)."""
    try:
        return ZoneInfo(asset.timezone)
    except ZoneInfoNotFoundError:
        _log.warning("Asset %r: unknown timezone %r — using UTC", asset.asset_id, asset.timezone)
        return ZoneInfo("UTC")


def effective_day_target(
    weekly: dict[int, EvWeeklyTarget],
    overrides: dict[date, EvDayOverride],
    day: date,
) -> tuple[float, str, str] | None:
    """Effective target for one local calendar date.

    Returns ``(target_soc_pct, target_by "HH:MM", source)`` where source is
    ``"override"`` or ``"weekly"``, or ``None`` when the day has no target
    (skipped, weekday disabled, or nothing planned).
    """
    ov = overrides.get(day)
    if ov is not None and ov.skip:
        return None

    wt = weekly.get(day.isoweekday())
    if ov is not None:
        soc = ov.target_soc_pct
        hhmm = ov.target_by
        # Fall back to the weekly row for whichever half is unset
        if soc is None and wt is not None and wt.enabled:
            soc = wt.target_soc_pct
        if hhmm is None and wt is not None and wt.enabled:
            hhmm = wt.target_by
        if soc is not None and hhmm is not None:
            return soc, hhmm, "override"
        return None

    if wt is not None and wt.enabled:
        return wt.target_soc_pct, wt.target_by, "weekly"
    return None


def _resolve_target(
    asset: EvChargingAsset,
    weekly: dict[int, EvWeeklyTarget],
    overrides: dict[date, EvDayOverride],
    now: datetime,
    current_soc: float,
    dismissed_day: date | None = None,
    catch_up_if_plugged_since: datetime | None | object = None,
) -> tuple[float, datetime, bool] | None:
    """Return the next (target_soc_pct, target_by UTC, overdue) deadline, or None.

    Walks forward from today (asset-local): the first day whose effective
    target has a deadline still in the future wins.  Skipped days and
    already-passed deadlines are stepped over — except *today's* deadline:
    if it has already passed but ``current_soc`` hasn't reached the target
    yet, it is returned anyway (``overdue=True``) so the caller keeps
    forcing the car toward today's target instead of silently rolling over
    to the next scheduled day.  This naturally stops at local midnight, once
    "today" advances past the missed deadline's date — or sooner, if
    *dismissed_day* (today's target was already met once) matches: the
    catch-up is then skipped and the walk moves on to the next scheduled
    day.

    *catch_up_if_plugged_since* is the car's plug-in time: the overdue
    catch-up only applies if it is at or before the missed deadline (the car
    was plugged in when it passed). ``None`` = not plugged in / unknown → no
    catch-up; ``_ALWAYS`` disables the check.
    """
    tz = asset_zoneinfo(asset)
    now_local = now.astimezone(tz)

    for days_ahead in range(8):
        day = (now_local + timedelta(days=days_ahead)).date()
        target = effective_day_target(weekly, overrides, day)
        if target is None:
            continue

        soc, hhmm, _source = target
        h, m = _parse_hhmm(hhmm)
        deadline_local = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
        if deadline_local <= now_local:
            deadline_utc = deadline_local.astimezone(timezone.utc)
            plugged_at_deadline = catch_up_if_plugged_since is _ALWAYS or (
                isinstance(catch_up_if_plugged_since, datetime)
                and catch_up_if_plugged_since <= deadline_utc
            )
            if (days_ahead == 0 and current_soc < soc and day != dismissed_day
                    and plugged_at_deadline):
                return soc, deadline_utc, True
            continue

        return soc, deadline_local.astimezone(timezone.utc), False

    return None


def upcoming_days(
    asset: EvChargingAsset,
    weekly: dict[int, EvWeeklyTarget],
    overrides: dict[date, EvDayOverride],
    now: datetime | None = None,
    days: int = 7,
) -> list[dict[str, Any]]:
    """Effective plan for the next *days* local calendar days (UI strip).

    Each entry: ``{date, weekday, source, target_soc_pct, target_by,
    deadline_utc, passed}`` — source is "weekly" | "override" | "skip" |
    "none"; passed marks deadlines already behind us (today, after the
    deadline).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    tz = asset_zoneinfo(asset)
    now_local = now.astimezone(tz)

    result: list[dict[str, Any]] = []
    for days_ahead in range(days):
        day = (now_local + timedelta(days=days_ahead)).date()
        ov = overrides.get(day)
        target = effective_day_target(weekly, overrides, day)

        entry: dict[str, Any] = {
            "date": day.isoformat(),
            "weekday": day.isoweekday(),
            "source": "none",
            "target_soc_pct": None,
            "target_by": None,
            "deadline_utc": None,
            "passed": False,
        }
        if ov is not None and ov.skip:
            entry["source"] = "skip"
        elif target is not None:
            soc, hhmm, source = target
            h, m = _parse_hhmm(hhmm)
            deadline_local = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            entry.update({
                "source": source,
                "target_soc_pct": soc,
                "target_by": hhmm,
                "deadline_utc": deadline_local.astimezone(timezone.utc).isoformat(),
                "passed": deadline_local <= now_local,
            })
        result.append(entry)
    return result


def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse "HH:MM" → (hour, minute)."""
    parts = s.split(":")
    return int(parts[0]), int(parts[1])

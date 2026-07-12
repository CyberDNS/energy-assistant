"""Parse the ``assets:`` config section and compute active ``EvChargingGoal`` objects.

Called by the planning loop on every optimization cycle so that the goals
always reflect the current SoC, the DB-backed weekly plan, and any dated
overrides/skips set in the UI.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.models import DeviceState, ThresholdConstraints
from .ev import (
    ChargeCurvePoint,
    EvChargingAsset,
    EvChargingGoal,
    EvDayOverride,
    EvWeeklyTarget,
    build_goal_from_parts,
)

_log = logging.getLogger(__name__)

_THRESHOLD_TYPE = "threshold"


def parse_ev_assets(raw_assets: dict[str, Any]) -> list[EvChargingAsset]:
    """Parse the ``assets:`` section of the YAML config into ``EvChargingAsset`` objects.

    Entries with ``type: threshold`` are silently skipped (handled by
    ``parse_threshold_assets``).
    """
    result: list[EvChargingAsset] = []
    for asset_id, cfg in raw_assets.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("type") == _THRESHOLD_TYPE:
            continue
        try:
            result.append(_parse_one(asset_id, cfg))
        except Exception as exc:  # noqa: BLE001
            _log.warning("assets[%r]: parse failed — %s", asset_id, exc)
    return result


def parse_threshold_assets(
    raw_assets: dict[str, Any],
    devices: dict[str, dict[str, Any]] | None = None,
) -> list[ThresholdConstraints]:
    """Parse ``type: threshold`` entries from the ``assets:`` config section.

    Each entry must supply:
    - ``device``            — device_id matching a registered device.
    - ``bottom_threshold``  — lower bound of the allowed range.
    - ``top_threshold``     — upper bound of the allowed range.
    - ``active_rate_per_h`` — rate of value change while running (units/h).
    - ``drift_rate_per_h``  — rate of natural drift when off (units/h).

    Optional:
    - ``rated_power_kw``    — electrical power when running (kW).  Falls back
      to the referenced device's ``rated_power_w`` (from *devices*) so the
      value only needs to be declared once, on the device.
    - ``unit``          — display unit (default "").
    - ``direction``     — "reduces" (default) or "increases".
    - ``min_runtime_h`` — minimum on-time for compressor protection (h).
    - ``min_offtime_h`` — minimum off-time for compressor protection (h).
    """
    result: list[ThresholdConstraints] = []
    for asset_id, cfg in raw_assets.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("type") != _THRESHOLD_TYPE:
            continue
        try:
            result.append(_parse_threshold_one(asset_id, cfg, devices or {}))
        except Exception as exc:  # noqa: BLE001
            _log.warning("assets[%r] (threshold): parse failed — %s", asset_id, exc)
    return result


def _parse_threshold_one(
    asset_id: str,
    cfg: dict[str, Any],
    devices: dict[str, dict[str, Any]],
) -> ThresholdConstraints:
    device_id = str(cfg.get("device") or asset_id)

    rated_power_kw = cfg.get("rated_power_kw")
    if rated_power_kw is None:
        rated_power_w = (devices.get(device_id) or {}).get("rated_power_w")
        if rated_power_w is None:
            raise ValueError(
                f"no rated_power_kw on the asset and no rated_power_w on device {device_id!r}"
            )
        rated_power_kw = float(rated_power_w) / 1000.0

    return ThresholdConstraints(
        device_id=device_id,
        bottom_threshold=float(cfg["bottom_threshold"]),
        top_threshold=float(cfg["top_threshold"]),
        unit=str(cfg.get("unit", "")),
        direction=str(cfg.get("direction", "reduces")),  # type: ignore[arg-type]
        rated_power_kw=float(rated_power_kw),
        active_rate_per_h=float(cfg["active_rate_per_h"]),
        drift_rate_per_h=float(cfg["drift_rate_per_h"]),
        min_runtime_h=float(cfg.get("min_runtime_h", 0.0)),
        min_offtime_h=float(cfg.get("min_offtime_h", 0.0)),
        label=str(cfg.get("label", "")),
    )


def _parse_one(asset_id: str, cfg: dict[str, Any]) -> EvChargingAsset:
    device_id = cfg["device"]
    capacity = float(cfg["capacity_kwh"])
    max_charge = float(cfg.get("max_charge_kw", 11.0))
    min_charge = float(cfg.get("min_charge_kw", 1.38))  # 6 A × 230 V single-phase default
    charge_limit = float(cfg.get("charge_limit_soc_pct", 100.0))
    label = str(cfg.get("label", asset_id))
    tz = str(cfg.get("timezone", "Europe/Berlin"))

    curve: list[ChargeCurvePoint] = []
    for pt in cfg.get("charge_curve", []):
        curve.append(ChargeCurvePoint(soc_pct=float(pt["soc_pct"]),
                                      efficiency=float(pt["efficiency"])))
    if not curve:
        # Default: full rate to 80%, 55% above 80%
        curve = [ChargeCurvePoint(80.0, 1.0), ChargeCurvePoint(100.0, 0.55)]

    return EvChargingAsset(
        asset_id=asset_id,
        device_id=device_id,
        label=label,
        capacity_kwh=capacity,
        max_charge_kw=max_charge,
        min_charge_kw=min_charge,
        charge_limit_soc_pct=charge_limit,
        charge_curve=curve,
        timezone=tz,
    )


def resolve_active_goals(
    assets: list[EvChargingAsset],
    device_states: dict[str, DeviceState],
    weekly_plans: dict[str, dict[int, EvWeeklyTarget]],
    day_overrides: dict[str, dict[date, EvDayOverride]],
    now: datetime | None = None,
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
    """
    if now is None:
        now = datetime.now(timezone.utc)

    goals: list[EvChargingGoal] = []
    for asset in assets:
        state = device_states.get(asset.device_id)
        connected = state is not None and state.available
        current_soc = (state.soc_pct or 0.0) if state is not None else 0.0

        target_info = _resolve_target(
            asset,
            weekly_plans.get(asset.asset_id, {}),
            day_overrides.get(asset.asset_id, {}),
            now,
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

        target_soc, target_by = target_info

        # Skip if already at target
        if current_soc >= target_soc:
            _log.debug("Asset %r: SoC %.0f%% ≥ target %.0f%% — no goal needed",
                       asset.asset_id, current_soc, target_soc)
            continue

        goals.append(build_goal_from_parts(
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
        ))

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
) -> tuple[float, datetime] | None:
    """Return the next (target_soc_pct, target_by UTC) deadline, or None.

    Walks forward from today (asset-local): the first day whose effective
    target has a deadline still in the future wins.  Skipped days and
    already-passed deadlines are stepped over.
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
            continue

        return soc, deadline_local.astimezone(timezone.utc)

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

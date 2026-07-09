"""Parse the ``assets:`` config section and compute active ``EvChargingGoal`` objects.

Called by the planning loop on every optimization cycle so that the goals
always reflect the current SoC, the current day's schedule entry, and any
live UI overrides.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.models import DeviceState, ThresholdConstraints
from .ev import (
    ChargeCurvePoint,
    EvChargingAsset,
    EvChargingGoal,
    EvScheduleEntry,
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


def parse_threshold_assets(raw_assets: dict[str, Any]) -> list[ThresholdConstraints]:
    """Parse ``type: threshold`` entries from the ``assets:`` config section.

    Each entry must supply:
    - ``device``            — device_id matching a registered device.
    - ``bottom_threshold``  — lower bound of the allowed range.
    - ``top_threshold``     — upper bound of the allowed range.
    - ``rated_power_kw``    — electrical power when running (kW).
    - ``active_rate_per_h`` — rate of value change while running (units/h).
    - ``drift_rate_per_h``  — rate of natural drift when off (units/h).

    Optional:
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
            result.append(_parse_threshold_one(asset_id, cfg))
        except Exception as exc:  # noqa: BLE001
            _log.warning("assets[%r] (threshold): parse failed — %s", asset_id, exc)
    return result


def _parse_threshold_one(asset_id: str, cfg: dict[str, Any]) -> ThresholdConstraints:
    device_id = cfg.get("device") or asset_id
    return ThresholdConstraints(
        device_id=str(device_id),
        bottom_threshold=float(cfg["bottom_threshold"]),
        top_threshold=float(cfg["top_threshold"]),
        unit=str(cfg.get("unit", "")),
        direction=str(cfg.get("direction", "reduces")),  # type: ignore[arg-type]
        rated_power_kw=float(cfg["rated_power_kw"]),
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

    schedule: list[EvScheduleEntry] = []
    for entry in cfg.get("schedule", []):
        schedule.append(EvScheduleEntry(
            days=[int(d) for d in entry["days"]],
            target_soc_pct=float(entry["target_soc_pct"]),
            target_by=str(entry["target_by"]),
        ))

    return EvChargingAsset(
        asset_id=asset_id,
        device_id=device_id,
        label=label,
        capacity_kwh=capacity,
        max_charge_kw=max_charge,
        min_charge_kw=min_charge,
        charge_limit_soc_pct=charge_limit,
        charge_curve=curve,
        schedule=schedule,
        timezone=tz,
    )


def resolve_active_goals(
    assets: list[EvChargingAsset],
    device_states: dict[str, DeviceState],
    overrides: dict[str, tuple[float, datetime]],
    now: datetime | None = None,
) -> list[EvChargingGoal]:
    """Compute an ``EvChargingGoal`` for every asset that has an active target.

    Parameters
    ----------
    assets:
        Parsed asset configs.
    device_states:
        Latest state per device (from the registry).
    overrides:
        UI overrides keyed by asset_id → (target_soc_pct, target_by UTC).
        If an override is present it takes precedence over the schedule.
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

        # Determine target from override or schedule
        target_info = _resolve_target(asset, overrides, now)
        if target_info is None:
            # No target → generate a "connected, no deadline" goal
            # (contributor will set PV Charging opportunistically)
            if connected:
                goals.append(build_goal_from_parts(
                    asset_id=asset.asset_id,
                    device_id=asset.device_id,
                    capacity_kwh=asset.capacity_kwh,
                    max_charge_kw=asset.max_charge_kw,
                    min_charge_kw=asset.min_charge_kw,
                    charge_limit_soc_pct=asset.charge_limit_soc_pct,
                    target_soc_pct=asset.charge_limit_soc_pct,
                    target_by=now + timedelta(hours=168),  # far future
                    charge_curve=asset.charge_curve,
                    current_soc_pct=current_soc,
                    connected=connected,
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


def _resolve_target(
    asset: EvChargingAsset,
    overrides: dict[str, tuple[float, datetime]],
    now: datetime,
) -> tuple[float, datetime] | None:
    """Return (target_soc_pct, target_by UTC) from override or schedule, or None."""
    if asset.asset_id in overrides:
        return overrides[asset.asset_id]

    try:
        tz = ZoneInfo(asset.timezone)
    except ZoneInfoNotFoundError:
        _log.warning("Asset %r: unknown timezone %r — using UTC", asset.asset_id, asset.timezone)
        tz = ZoneInfo("UTC")

    now_local = now.astimezone(tz)

    # Search up to 7 days ahead for the next applicable schedule entry
    for days_ahead in range(8):
        candidate_date = now_local + timedelta(days=days_ahead)
        iso_weekday = candidate_date.isoweekday()  # 1=Mon…7=Sun

        for entry in asset.schedule:
            if iso_weekday not in entry.days:
                continue

            h, m = _parse_hhmm(entry.target_by)
            deadline_local = candidate_date.replace(
                hour=h, minute=m, second=0, microsecond=0
            )

            # Skip deadlines that have already passed
            if deadline_local <= now_local:
                continue

            deadline_utc = deadline_local.astimezone(timezone.utc)
            return entry.target_soc_pct, deadline_utc

    return None


def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse "HH:MM" → (hour, minute)."""
    parts = s.split(":")
    return int(parts[0]), int(parts[1])

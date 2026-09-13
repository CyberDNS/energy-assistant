"""EV charging asset models, charge-curve math, and the EvChargerContributor.

Charge curve
------------
A piecewise model of how charging efficiency (kWh delivered to battery /
kWh drawn from wall) decreases at high SoC due to CC/CV taper.  Each
``ChargeCurvePoint`` defines the efficiency for the *segment ending* at that
SoC.  The implicit first segment starts at 0 %.

Example config::

    charge_curve:
      - soc_pct: 80   # 0 → 80 %: full rate
        efficiency: 1.0
      - soc_pct: 100  # 80 → 100 %: ~1.8 × longer per kWh
        efficiency: 0.55

Charger mode encoding
---------------------
``EvChargerContributor.desired_setpoint_w`` returns a sentinel float that
``OpenWBDevice.send_command`` interprets as an openWB charging mode:

  value > 500 W  → "Instant Charging" (charges at max_charge_kw)
  0 < value ≤ 500 W  → "PV Charging"  (openWB manages surplus & phases)
  value == 0.0   → "Stop"
  None           → no command sent (car not connected, or chargepoint disabled)

A disabled chargepoint always returns None: the assistant is fully hands-off
so the wallbox (or the user via the openWB UI) controls charging itself.
Disabling does not stop an in-progress charge — the last commanded mode
simply remains under wallbox control.

PV priority between multiple EVs: when the optimizer allocated the PV
surplus of the current slot to one EV (``LiveSituation.ev_pv_planned_ids``),
EVs *without* a planned charging slot yield by commanding Stop instead of
opportunistic PV mode — otherwise both wallboxes sit in PV mode and openWB
splits the surplus by its own priority rules, overriding the plan.

This encoding stays inside the existing ``ControlContributor`` protocol
(``desired_setpoint_w`` returns ``float | None``) without changing the
control loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.control import ControlIntent, LiveSituation

_log = logging.getLogger(__name__)

# Sentinel values sent by desired_setpoint_w and interpreted by OpenWBDevice.
_INSTANT_SENTINEL_W = 11_000.0   # > 500 W → Instant Charging
_PV_SENTINEL_W = 1.0             # 0 < x ≤ 500 W → PV Charging
_STOP_W = 0.0                    # == 0 → Stop
_MODE_THRESHOLD_W = 500.0        # boundary between PV and Instant


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ChargeCurvePoint:
    """Efficiency at the end of a charging segment (see module docstring)."""

    soc_pct: float
    efficiency: float


@dataclass
class EvWeeklyTarget:
    """One weekday's default charging target — DB-backed, edited in the UI.

    Exactly one (optional) target per ISO weekday per asset.  A disabled
    weekday means "no charging deadline that day" (the EV still absorbs PV
    opportunistically).
    """

    weekday: int         # ISO weekday: 1=Mon … 7=Sun
    enabled: bool
    target_soc_pct: float
    target_by: str       # "HH:MM" in the asset's local timezone


@dataclass
class EvDayOverride:
    """Ephemeral per-date deviation from the weekly plan.

    Keyed by the deadline's local calendar date.  Either a skip (no target
    that day) or a replacement target.  Rows stay valid for their entire
    calendar day — the UI keeps showing "skipped" after the deadline passed —
    and are purged after local midnight.
    """

    date: date
    skip: bool = False
    target_soc_pct: float | None = None
    target_by: str | None = None   # "HH:MM" local; None → keep weekly time


@dataclass
class EvChargingAsset:
    """Static configuration for one EV chargepoint (from config.yaml)."""

    asset_id: str
    device_id: str
    label: str
    capacity_kwh: float
    max_charge_kw: float
    min_charge_kw: float = 1.38   # 6 A × 230 V single-phase; override per charger
    charge_limit_soc_pct: float = 100.0
    charge_curve: list[ChargeCurvePoint] = field(default_factory=list)
    timezone: str = "Europe/Berlin"


@dataclass
class EvChargingGoal:
    """Active charging target for one EV — computed by the asset loader.

    Either derived from the weekly schedule or set by a UI override.
    Passed into ``OptimizationContext`` so the MILP can plan around it.
    """

    asset_id: str
    device_id: str
    capacity_kwh: float
    max_charge_kw: float
    min_charge_kw: float
    charge_limit_soc_pct: float
    target_soc_pct: float
    target_by: datetime          # UTC
    charge_curve: list[ChargeCurvePoint]
    current_soc_pct: float
    connected: bool
    # Pre-computed by loader
    phase1_required_kwh: float   # current_soc → charge_limit, wall energy
    phase2_required_kwh: float   # charge_limit → target_soc, wall energy
    phase2_duration_h: float     # time at max_charge_kw to complete phase2
    phase2_start_time: datetime  # = target_by − phase2_duration_h, UTC
    # True when no schedule is active: MILP plans PV-only absorption; execution uses PV sentinel
    pv_only: bool = False
    # True when the remaining energy can no longer be delivered by target_by
    # even at max_charge_kw — phase1/phase2 are merged into one mandatory
    # full-power block starting now (see build_goal_from_parts).
    infeasible: bool = False
    # True when target_by is already in the past and the target still isn't
    # met — the deadline was missed and we keep forcing full power to catch
    # up rather than silently rolling over to the next scheduled day.
    overdue: bool = False


# ---------------------------------------------------------------------------
# Charge-curve helpers
# ---------------------------------------------------------------------------


def compute_wall_kwh(
    from_soc_pct: float,
    to_soc_pct: float,
    capacity_kwh: float,
    charge_curve: list[ChargeCurvePoint],
) -> float:
    """Wall energy (kWh) needed to charge from *from_soc_pct* to *to_soc_pct*.

    Uses piecewise efficiency from *charge_curve*.  Returns 0.0 when
    ``from_soc_pct >= to_soc_pct``.
    """
    if from_soc_pct >= to_soc_pct or capacity_kwh <= 0:
        return 0.0

    sorted_curve = sorted(charge_curve, key=lambda p: p.soc_pct)

    # Build segments: (seg_start%, seg_end%, efficiency)
    segments: list[tuple[float, float, float]] = []
    prev = 0.0
    for pt in sorted_curve:
        segments.append((prev, pt.soc_pct, pt.efficiency))
        prev = pt.soc_pct
    if prev < 100.0:
        last_eff = sorted_curve[-1].efficiency if sorted_curve else 1.0
        segments.append((prev, 100.0, last_eff))

    total = 0.0
    for seg_start, seg_end, eff in segments:
        start = max(seg_start, from_soc_pct)
        end = min(seg_end, to_soc_pct)
        if end <= start:
            continue
        nominal = (end - start) / 100.0 * capacity_kwh
        total += nominal / max(eff, 0.01)

    return total


def build_goal_from_parts(
    asset_id: str,
    device_id: str,
    capacity_kwh: float,
    max_charge_kw: float,
    min_charge_kw: float,
    charge_limit_soc_pct: float,
    target_soc_pct: float,
    target_by: datetime,
    charge_curve: list[ChargeCurvePoint],
    current_soc_pct: float,
    connected: bool,
    pv_only: bool = False,
    now: datetime | None = None,
    overdue: bool = False,
) -> EvChargingGoal:
    """Construct an ``EvChargingGoal`` with pre-computed phase fields.

    When *now* is given, the goal is checked for feasibility: if the
    remaining energy (phase1 + phase2) can no longer be delivered by
    ``target_by`` even at ``max_charge_kw``, phase1 and phase2 are merged
    into a single mandatory full-power block starting *now* instead of the
    economically-optimized phase1 the MILP would otherwise plan — there is
    no time budget left to be clever about it.  *overdue* forces the same
    merge unconditionally (used when ``target_by`` itself is already in the
    past and the target still hasn't been reached).
    """
    effective_limit = min(charge_limit_soc_pct, target_soc_pct)

    phase1_kwh = compute_wall_kwh(
        current_soc_pct, effective_limit, capacity_kwh, charge_curve
    )
    phase2_kwh = compute_wall_kwh(
        effective_limit, target_soc_pct, capacity_kwh, charge_curve
    )
    phase2_h = phase2_kwh / max_charge_kw if max_charge_kw > 0 else 0.0
    phase2_start = target_by - timedelta(hours=phase2_h)

    infeasible = False
    if now is not None and not overdue and (phase1_kwh + phase2_kwh) > 0.01:
        available_h = (target_by - now).total_seconds() / 3600.0
        deliverable_kwh = max_charge_kw * max(0.0, available_h)
        if available_h <= 0 or (phase1_kwh + phase2_kwh) > deliverable_kwh + 0.01:
            infeasible = True

    if (infeasible or overdue) and (phase1_kwh + phase2_kwh) > 0.01:
        # No time left to distinguish phase1/phase2 — force full power for
        # everything still needed, starting immediately.
        phase1_kwh, phase2_kwh = 0.0, phase1_kwh + phase2_kwh
        phase2_h = phase2_kwh / max_charge_kw if max_charge_kw > 0 else 0.0
        phase2_start = now if now is not None else phase2_start

    return EvChargingGoal(
        asset_id=asset_id,
        device_id=device_id,
        capacity_kwh=capacity_kwh,
        max_charge_kw=max_charge_kw,
        min_charge_kw=min_charge_kw,
        charge_limit_soc_pct=effective_limit,
        target_soc_pct=target_soc_pct,
        target_by=target_by,
        charge_curve=charge_curve,
        current_soc_pct=current_soc_pct,
        connected=connected,
        phase1_required_kwh=phase1_kwh,
        phase2_required_kwh=phase2_kwh,
        phase2_duration_h=phase2_h,
        phase2_start_time=phase2_start,
        pv_only=pv_only,
        infeasible=infeasible,
        overdue=overdue,
    )


# ---------------------------------------------------------------------------
# Control contributor
# ---------------------------------------------------------------------------


class EvChargerContributor:
    """``ControlContributor`` for an openWB EV chargepoint.

    Translates optimizer intents into the three openWB modes:
    Instant Charging / PV Charging / Stop.

    The active goal is injected externally via ``update_goal()`` — the
    planning loop calls this after each optimizer run and whenever a UI
    override is applied.
    """

    # Marks EV contributors for the control loop's PV-priority resolution
    # (see LiveSituation.ev_pv_planned_ids) without a core → assets import.
    is_ev = True

    def __init__(self, asset: EvChargingAsset) -> None:
        self._asset = asset
        self._active_goal: EvChargingGoal | None = None
        self._disabled: bool = False
        self._force_target_soc: float | None = None
        # (setpoint, reason) last logged — only log on change so the normal
        # 30 s control tick doesn't spam the log while nothing changed.
        self._last_logged: tuple[float | None, str] | None = None

    @property
    def device_id(self) -> str:
        return self._asset.device_id

    def update_goal(self, goal: EvChargingGoal | None) -> None:
        """Replace the current charging goal (called by the planning loop)."""
        self._active_goal = goal

    def set_disabled(self, disabled: bool) -> None:
        """When disabled the contributor sends no commands and is excluded from planning."""
        self._disabled = disabled

    def set_force_charge(self, target_soc_pct: float | None) -> None:
        """Activate (or clear with ``None``) forced full-speed charging.

        While active the contributor bypasses plan and goals entirely and
        commands Instant Charging until the target SoC is reached.  The
        Application clears the flag on vehicle unplug or target reached.
        """
        self._force_target_soc = target_soc_pct

    @property
    def force_charge_target_soc(self) -> float | None:
        return self._force_target_soc

    def _decide(
        self,
        value: float | None,
        reason: str,
        *,
        current_soc: float | None = None,
        goal: "EvChargingGoal | None" = None,
    ) -> float | None:
        """Return *value*, logging a line whenever (value, reason) changes.

        Every decision path in ``desired_setpoint_w`` routes through here so
        the log carries a full audit trail of mode changes — which branch
        fired, and why — without spamming on every 30 s control tick while
        nothing actually changed.
        """
        key = (value, reason)
        if key != self._last_logged:
            self._last_logged = key
            detail = ""
            if goal is not None:
                detail = (
                    f" [target={goal.target_soc_pct:.0f}% by {goal.target_by.isoformat()}"
                    f" phase1={goal.phase1_required_kwh:.1f}kWh"
                    f" phase2={goal.phase2_required_kwh:.1f}kWh"
                    f" infeasible={goal.infeasible} overdue={goal.overdue}]"
                )
            soc_str = f"{current_soc:.0f}%" if current_soc is not None else "?"
            _log.info(
                "EV %r (%s): soc=%s → setpoint=%s — %s%s",
                self._asset.asset_id, self.device_id, soc_str, value, reason, detail,
            )
        return value

    def desired_setpoint_w(
        self,
        intent: "ControlIntent | None",
        live: "LiveSituation",
    ) -> float | None:
        """Return a mode-encoding sentinel (see module docstring)."""
        if self._disabled:
            # Chargepoint disabled — hands off entirely, not even Stop.
            # The user hands control to the wallbox itself (manual mode
            # selection in openWB); the assistant must not intervene.
            return self._decide(None, "chargepoint disabled — hands off")
        state = live.device_states.get(self.device_id)
        if state is None or not state.available:
            # car not connected — don't send any command
            return self._decide(None, "not connected")

        goal = self._active_goal
        current_soc = state.soc_pct if state.soc_pct is not None else 0.0

        # Force charge overrides everything: full speed until the chosen
        # target.  Stop (not PV) once reached — the Application clears the
        # flag shortly after, returning control to the normal plan.
        if self._force_target_soc is not None:
            if current_soc >= self._force_target_soc:
                return self._decide(
                    _STOP_W, f"force-charge target {self._force_target_soc:.0f}% reached",
                    current_soc=current_soc,
                )
            return self._decide(
                self._asset.max_charge_kw * 1000.0,
                f"force-charge instant → {self._force_target_soc:.0f}%",
                current_soc=current_soc,
            )

        # Target fully met → Stop
        if goal is not None and current_soc >= goal.target_soc_pct:
            return self._decide(
                _STOP_W, f"target {goal.target_soc_pct:.0f}% reached",
                current_soc=current_soc, goal=goal,
            )

        # In forced top-off window (phase2) → Instant Charging, always
        if goal is not None and goal.phase2_required_kwh > 0.01:
            if live.timestamp >= goal.phase2_start_time:
                if goal.overdue:
                    reason = "forced full power — deadline already missed, catching up"
                elif goal.infeasible:
                    reason = "forced full power — target unreachable at max power, starting now"
                else:
                    reason = "phase2 top-off window — forced instant charging"
                return self._decide(
                    self._asset.max_charge_kw * 1000.0, reason,
                    current_soc=current_soc, goal=goal,
                )

        # At charge limit but phase2 window not yet open → hold (Stop)
        if goal is not None and current_soc >= goal.charge_limit_soc_pct:
            return self._decide(
                _STOP_W,
                f"at charge_limit {goal.charge_limit_soc_pct:.0f}% — holding until "
                f"phase2 opens at {goal.phase2_start_time.isoformat()}",
                current_soc=current_soc, goal=goal,
            )

        # When another EV holds the PV allocation for this slot, yield:
        # command Stop instead of opportunistic PV so openWB gives the whole
        # surplus to the chargepoint the optimizer planned it for.
        others_hold_pv = bool(live.ev_pv_planned_ids - {self.device_id})

        # No active goal → opportunistic PV charging (unless yielding)
        if goal is None:
            if others_hold_pv:
                return self._decide(
                    _STOP_W, "no goal — yielding PV to another EV", current_soc=current_soc,
                )
            return self._decide(
                _PV_SENTINEL_W, "no goal — opportunistic PV", current_soc=current_soc,
            )

        # Optimizer planned a charging step
        if intent is not None and intent.power_kw > 0:
            if goal.pv_only or not intent.grid_allowed:
                # PV-sourced slot (or no-schedule goal): the plan shows an
                # estimated kW, but execution must not draw from the grid —
                # PV mode lets openWB track the real live surplus.  Instant
                # charging would pull the shortfall from the grid.
                return self._decide(
                    _PV_SENTINEL_W, "plan: PV-sourced slot",
                    current_soc=current_soc, goal=goal,
                )
            planned = intent.power_kw
            setpoint = max(self._asset.min_charge_kw, min(self._asset.max_charge_kw, planned)) * 1000.0
            return self._decide(
                setpoint, f"plan: grid-sourced {planned:.1f} kW",
                current_soc=current_soc, goal=goal,
            )

        # Idle or no intent → opportunistic PV charging (unless yielding)
        if others_hold_pv:
            return self._decide(
                _STOP_W, "idle — yielding PV to another EV",
                current_soc=current_soc, goal=goal,
            )
        return self._decide(
            _PV_SENTINEL_W, "idle — opportunistic PV",
            current_soc=current_soc, goal=goal,
        )

    def charge_price_eur_per_kwh(
        self,
        intent: "ControlIntent | None",
        live: "LiveSituation",
    ) -> float:
        return live.market_price_eur_per_kwh

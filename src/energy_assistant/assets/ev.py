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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.control import ControlIntent, LiveSituation

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
class EvScheduleEntry:
    """One row of a vehicle's weekly charging schedule."""

    days: list[int]      # ISO weekday: 1=Mon … 7=Sun
    target_soc_pct: float
    target_by: str       # "HH:MM" in the asset's local timezone


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
    schedule: list[EvScheduleEntry] = field(default_factory=list)
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
) -> EvChargingGoal:
    """Construct an ``EvChargingGoal`` with pre-computed phase fields."""
    effective_limit = min(charge_limit_soc_pct, target_soc_pct)

    phase1_kwh = compute_wall_kwh(
        current_soc_pct, effective_limit, capacity_kwh, charge_curve
    )
    phase2_kwh = compute_wall_kwh(
        effective_limit, target_soc_pct, capacity_kwh, charge_curve
    )
    phase2_h = phase2_kwh / max_charge_kw if max_charge_kw > 0 else 0.0
    phase2_start = target_by - timedelta(hours=phase2_h)

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

    @property
    def device_id(self) -> str:
        return self._asset.device_id

    def update_goal(self, goal: EvChargingGoal | None) -> None:
        """Replace the current charging goal (called by the planning loop)."""
        self._active_goal = goal

    def set_disabled(self, disabled: bool) -> None:
        """When disabled the contributor sends no commands and is excluded from planning."""
        self._disabled = disabled

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
            return None
        state = live.device_states.get(self.device_id)
        if state is None or not state.available:
            return None  # car not connected — don't send any command

        goal = self._active_goal
        current_soc = state.soc_pct if state.soc_pct is not None else 0.0

        # Target fully met → Stop
        if goal is not None and current_soc >= goal.target_soc_pct:
            return _STOP_W

        # In forced top-off window (phase2) → Instant Charging, always
        if goal is not None and goal.phase2_required_kwh > 0.01:
            if live.timestamp >= goal.phase2_start_time:
                return self._asset.max_charge_kw * 1000.0

        # At charge limit but phase2 window not yet open → hold (Stop)
        if goal is not None and current_soc >= goal.charge_limit_soc_pct:
            return _STOP_W

        # When another EV holds the PV allocation for this slot, yield:
        # command Stop instead of opportunistic PV so openWB gives the whole
        # surplus to the chargepoint the optimizer planned it for.
        others_hold_pv = bool(live.ev_pv_planned_ids - {self.device_id})

        # No active goal → opportunistic PV charging (unless yielding)
        if goal is None:
            return _STOP_W if others_hold_pv else _PV_SENTINEL_W

        # Optimizer planned a charging step
        if intent is not None and intent.power_kw > 0:
            if goal.pv_only or not intent.grid_allowed:
                # PV-sourced slot (or no-schedule goal): the plan shows an
                # estimated kW, but execution must not draw from the grid —
                # PV mode lets openWB track the real live surplus.  Instant
                # charging would pull the shortfall from the grid.
                return _PV_SENTINEL_W
            planned = intent.power_kw
            return max(self._asset.min_charge_kw, min(self._asset.max_charge_kw, planned)) * 1000.0

        # Idle or no intent → opportunistic PV charging (unless yielding)
        return _STOP_W if others_hold_pv else _PV_SENTINEL_W

    def charge_price_eur_per_kwh(
        self,
        intent: "ControlIntent | None",
        live: "LiveSituation",
    ) -> float:
        return live.market_price_eur_per_kwh

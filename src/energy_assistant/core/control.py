"""Control loop — executes the active EnergyPlan against live device states.

Architecture overview
---------------------
The fast control loop runs on a short interval (e.g. every 30 s).  On each
tick it:

1. Receives a ``LiveSituation`` snapshot (grid power, spot price, elapsed dt).
2. Finds the *active* ``ControlIntent`` for each registered contributor
   (the most recent intent whose ``timestep ≤ now``).
3. Asks each contributor for its **desired power setpoint**.
4. Sends a ``DeviceCommand(set_power_w=…)`` for every non-None setpoint.
5. Updates the ``BatteryCostLedger`` based on the *actual* measured power
   of each controlled device.
6. Applies gradual spot-floor decay to every storage contributor.

Contributor model
-----------------
Any device that participates in the fast loop implements ``ControlContributor``.
The protocol is intentionally minimal so that storage devices, EV chargers,
heat pumps, and other controllable loads can all plug in the same way without
changing the loop itself.

Current implementations
~~~~~~~~~~~~~~~~~~~~~~~
``StorageControlContributor``
    Battery / home storage.  Follows ``grid_fill`` / ``discharge`` intents
    from the MILP optimizer and absorbs PV overflow when idle.

Future extension points
~~~~~~~~~~~~~~~~~~~~~~~
To add EV chargers, heat pumps, or other controllable loads:
- Implement ``ControlContributor`` (duck-typed; no inheritance needed).
- Supply the contributor's charge price via ``price_eur_per_kwh()`` so the
  ledger update uses the right marginal cost (e.g. export price for PV
  overflow vs. import price for grid-fill).
- Register with ``ControlLoop.register_contributor()``.

No changes to ``ControlLoop`` itself are required.

PV overflow
-----------
When the optimizer issues an ``idle`` intent (or no plan covers the current
slot), ``StorageControlContributor`` inspects ``live.grid_power_w``:

- ``grid_power_w < 0`` (grid is exporting surplus): absorb up to
  ``min(|surplus|, max_charge_kw × 1000)`` W in the battery.
- Otherwise: send 0 W (hold charge).

This keeps the battery opportunistically full during sunny periods without
the optimizer needing to forecast every PV spike.

Ledger update
-------------
After sending commands the loop reads back ``state.power_w`` for each
contributor and records the actual energy flow in ``BatteryCostLedger``:

- Positive power (charging): ``record_charge(price=intent_price)``
- Negative power (discharging): ``record_discharge``
- Zero / unavailable: no ledger update

The effective charge price passed to the ledger depends on *how* the energy
arrived:
- ``grid_fill`` intent → ``live.current_price_eur_per_kwh`` (grid import)
- ``idle`` / PV overflow → ``live.pv_opportunity_price_eur_per_kwh``
  (the export rate you forgo by self-consuming; often 0 or feed-in tariff)

After all devices are updated, gradual spot-floor decay is applied to every
``StorageControlContributor``'s ledger entry using the battery's max charge
rate and the tick duration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .ledger import BatteryCostLedger
from .models import ControlIntent, DeviceCommand, EnergyPlan, DeviceState, StorageConstraints
from .realtime_slice_optimizer import StorageSliceInput, optimize_storage_slice

if TYPE_CHECKING:
    from .registry import DeviceRegistry

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Live situation snapshot
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class LiveSituation:
    """Snapshot of the energy situation at a single control tick.

    Built by the outer orchestration layer and passed unchanged to
    ``ControlLoop.tick()``.  All contributors receive the same snapshot.
    """

    timestamp: datetime
    """Wall-clock time of this tick (UTC)."""

    grid_power_w: float
    """Net grid power in W.  Positive = importing; negative = exporting (PV surplus)."""

    dt_hours: float
    """Time elapsed since the previous tick, in hours.

    Used by the ledger to compute energy from average power and by the
    spot-floor decay to advance the exponential decay by the right amount.
    """

    device_states: dict[str, DeviceState] = field(default_factory=dict)
    """Latest measured state per device, keyed by ``device_id``."""

    current_price_eur_per_kwh: float = 0.0
    """Current spot / tariff price for grid import (€/kWh).

    Applied to the ledger when a storage device charges from the grid
    (``grid_fill`` mode).
    """

    pv_opportunity_price_eur_per_kwh: float = 0.0
    """Opportunity cost of self-consuming PV instead of exporting it (€/kWh).

    Typically the feed-in tariff (e.g. 0.08 €/kWh).  When PV surplus charges
    the battery in ``idle`` / overflow mode, the ledger records this as the
    effective charge price — the cost of holding each kWh is what you gave
    up by not selling it to the grid.

    Defaults to 0.0 (no feed-in tariff / no opportunity cost).
    """

    pv_power_w: float = 0.0
    """Current PV production in W (positive = generating).

    Used to compute the site-level blended market price.  Defaults to 0.0
    when no PV device is available.
    """

    storage_cost_bases: dict[str, float] = field(default_factory=dict)
    """Cost basis (€/kWh) per storage device, from the BatteryCostLedger.

    Keyed by ``device_id``.  Used by ``market_price_eur_per_kwh`` to include
    the opportunity cost of discharging batteries as a third supply source.
    """

    default_zone_grid_power_w: float | None = None
    """Grid import power (W) for the default tariff zone, if known.

    When set (typically to the reading of the sub-meter for the default tariff,
    e.g. ``household_meter`` / Z2 in Messkonzept 8), ``market_price_eur_per_kwh``
    uses this value instead of the site-level ``grid_power_w`` (Z1).

    This matters when different circuits are billed under different tariffs
    (e.g. heatpump on flat rate, household on Tibber spot): using Z1 for the
    household battery charge price would inflate the grid fraction when the
    heatpump is running, even though the household circuit may be fully PV-powered.
    """

    @property
    def market_price_eur_per_kwh(self) -> float:
        """Blended market price for the default (household) tariff (€/kWh).

        Three supply sources are considered:

        1. **PV** (opportunity cost = feed-in price)
        2. **Grid import** (cost = default import tariff, e.g. Tibber spot)
        3. **Battery discharge** (cost = weighted-average ledger cost basis)

        Uses ``default_zone_grid_power_w`` (e.g. Z2 / household sub-meter)
        when available, falling back to the site-level ``grid_power_w`` (Z1).
        This avoids inflating the grid fraction when another tariff zone
        (e.g. heatpump) happens to be drawing from the grid simultaneously.
        """
        bat_w_total = 0.0
        bat_cost_total = 0.0
        for dev_id, basis in self.storage_cost_bases.items():
            state = self.device_states.get(dev_id)
            if state is not None and state.power_w is not None and state.power_w < 0.0:
                discharge_w = abs(state.power_w)
                bat_w_total += discharge_w
                bat_cost_total += discharge_w * basis

        pv_w = self.pv_power_w
        grid_ref = self.default_zone_grid_power_w if self.default_zone_grid_power_w is not None else self.grid_power_w
        grid_w = max(0.0, grid_ref)
        total_w = pv_w + grid_w + bat_w_total
        if total_w <= 0.0:
            return self.current_price_eur_per_kwh

        pv_frac   = min(1.0, pv_w / total_w)
        grid_frac = grid_w / total_w
        bat_frac  = bat_w_total / total_w
        bat_basis = bat_cost_total / bat_w_total if bat_w_total > 0.0 else 0.0

        return (
            pv_frac   * self.pv_opportunity_price_eur_per_kwh
            + grid_frac * self.current_price_eur_per_kwh
            + bat_frac  * bat_basis
        )


# ──────────────────────────────────────────────────────────────────────────────
# ControlContributor protocol
# ──────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class ControlContributor(Protocol):
    """Protocol for any device that participates in the fast control loop.

    Duck-typed — implementations do not inherit from this class.  Any object
    that provides these two members satisfies the protocol.

    Extension guide
    ---------------
    To add a new device category (e.g. EV charger, heat pump):

    1. Create a class with ``device_id`` property and ``desired_setpoint_w``
       method matching the signatures below.
    2. Optionally override ``charge_price_eur_per_kwh`` to return the
       marginal cost of the energy you're commanding (default: use
       ``live.current_price_eur_per_kwh``).  This is important for EV
       chargers that may have a separate tariff.
    3. ``register_contributor(contributor)`` on the ``ControlLoop`` instance.

    No other changes are needed.
    """

    @property
    def device_id(self) -> str:
        """Stable identifier of the device this contributor controls."""
        ...

    def desired_setpoint_w(
        self,
        intent: ControlIntent | None,
        live: LiveSituation,
    ) -> float | None:
        """Return the desired power setpoint in W, or ``None`` to skip.

        Sign convention
        ---------------
        Positive = charging / consuming.
        Negative = discharging / generating.

        Returning ``None`` means "send no command this tick" (the device
        keeps its current behaviour).
        """
        ...

    def charge_price_eur_per_kwh(self, intent: ControlIntent | None, live: LiveSituation) -> float:
        """Effective marginal cost (€/kWh) for energy charged this tick.

        The default implementation returns the site-level blended market price
        (PV fraction × feed-in + grid fraction × import).  Override to use a
        device-specific tariff (e.g. a heat-pump with a separate flat rate).
        """
        return live.market_price_eur_per_kwh


# ──────────────────────────────────────────────────────────────────────────────
# StorageControlContributor
# ──────────────────────────────────────────────────────────────────────────────


class StorageControlContributor:
    """Control contributor for battery / home-storage devices.

    Execution rules per intent mode
    --------------------------------
    ``grid_fill``
        Charge at the optimizer-planned rate: ``setpoint = intent.max_power_w``.
    ``discharge``
        Discharge at the optimizer-planned rate: ``setpoint = intent.min_power_w``
        (which is ≤ 0 by platform convention).
    ``idle`` / ``None`` (no covering intent)
        **PV overflow**: if the grid is currently exporting
        (``live.grid_power_w < 0``), absorb up to
        ``min(|surplus|, max_charge_kw × 1000)`` W.
        Otherwise hold at 0 W (no charge, no discharge).

    The actual setpoint sent to the device respects physical limits
    declared in ``StorageConstraints`` (``max_charge_kw``).
    """

    def __init__(self, constraints: StorageConstraints) -> None:
        self._constraints = constraints

    # ------------------------------------------------------------------
    # ControlContributor protocol
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._constraints.device_id

    def desired_setpoint_w(
        self,
        intent: ControlIntent | None,
        live: LiveSituation,
    ) -> float | None:
        mode = intent.mode if intent is not None else "idle"
        charge_policy = self._resolve_charge_policy(intent)

        if mode == "grid_fill":
            if intent is None or intent.max_power_w is None:
                return None
            planned_w = max(0.0, intent.max_power_w)
            if charge_policy == "pv_only":
                # Follow plan, but only up to currently available surplus.
                surplus_w = max(0.0, -live.grid_power_w)
                return min(planned_w, surplus_w)
            # grid_allowed / grid_only: follow optimizer setpoint.
            return planned_w

        if mode == "discharge":
            # Discharge at the optimizer-planned lower bound (≤ 0 W).
            return intent.min_power_w if intent is not None else None

        # idle (or unknown mode): opportunistic PV absorption
        surplus_w = -live.grid_power_w  # positive when grid is exporting
        if surplus_w > 1.0:
            max_charge_w = self._constraints.max_charge_kw * 1000.0
            return min(surplus_w, max_charge_w)
        return 0.0

    def _resolve_charge_policy(self, intent: ControlIntent | None) -> str:
        policy = (intent.charge_policy if intent is not None else "auto") or "auto"
        if policy == "auto":
            # Device capability remains the ultimate source constraint.
            return "pv_only" if self._constraints.no_grid_charge else "grid_allowed"
        return policy

    def charge_price_eur_per_kwh(
        self, intent: ControlIntent | None, live: LiveSituation
    ) -> float:
        """Blended market price (€/kWh) for energy stored this tick.

        Uses ``live.market_price_eur_per_kwh`` — the site-level blend of
        PV feed-in price and grid import price, weighted by each source's
        share of total site consumption:

            total_w     = pv_power_w + max(0, grid_power_w)
            pv_fraction = pv_power_w / total_w
            price       = pv_fraction × feed_in + (1 − pv_fraction) × tibber

        Example: PV=750W, grid=250W → 75% feed-in, 25% Tibber.  This is the
        same price that applies to any other load on the site at this moment.
        """
        return live.market_price_eur_per_kwh


# ──────────────────────────────────────────────────────────────────────────────
# ControlLoop
# ──────────────────────────────────────────────────────────────────────────────


class ControlLoop:
    """Fast control loop that executes an ``EnergyPlan`` against live device states.

    Typical usage
    -------------
    ::

        ledger = BatteryCostLedger()
        loop = ControlLoop(ledger=ledger)
        loop.register_contributor(StorageControlContributor(battery_constraints))

        # Subscribe to new plans:
        event_bus.subscribe(PlanUpdatedEvent, lambda e: loop.update_plan(e.plan))

        # In the scheduler / async task:
        while True:
            live = LiveSituation(
                timestamp=datetime.now(timezone.utc),
                grid_power_w=grid_meter.power_w,
                dt_hours=30 / 3600,
                device_states=registry.latest_states(),
                current_price_eur_per_kwh=tariff.current_price(),
                pv_opportunity_price_eur_per_kwh=tariff.export_price(),
            )
            await loop.tick(live, registry)
            await asyncio.sleep(30)

    Modularity
    ----------
    Add new device types by calling ``register_contributor()`` with any object
    satisfying the ``ControlContributor`` protocol.  The loop is unaware of
    device specifics — it only calls ``desired_setpoint_w`` and routes the
    resulting command to the registry.
    """

    def __init__(
        self,
        ledger: BatteryCostLedger,
        contributors: list[ControlContributor] | None = None,
    ) -> None:
        self._ledger = ledger
        self._contributors: list[ControlContributor] = list(contributors or [])
        self._active_plan: EnergyPlan | None = None
        self._last_storage_setpoints_w: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Plan management
    # ------------------------------------------------------------------

    def update_plan(self, plan: EnergyPlan) -> None:
        """Replace the current EnergyPlan.

        Call this from a ``PlanUpdatedEvent`` subscriber.  The change
        takes effect on the *next* tick — any command already in flight
        for the current tick is not affected.
        """
        self._active_plan = plan
        _log.info(
            "ControlLoop: new plan  created_at=%s  intents=%d",
            plan.created_at.isoformat(),
            len(plan.intents),
        )
        # Log the next 2 upcoming intents per device so the operator can see
        # what the optimizer has decided without digging into the plan object.
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            (i for i in plan.intents if i.timestep >= now),
            key=lambda i: i.timestep,
        )
        slots_logged: dict[str, int] = {}
        for intent in upcoming:
            if slots_logged.get(intent.device_id, 0) >= 2:
                continue
            slots_logged[intent.device_id] = slots_logged.get(intent.device_id, 0) + 1
            _log.info(
                "  plan  %s  %-14s  mode=%-10s  planned=%+.0f W  bounds=[%.0f … %.0f W]",
                intent.timestep.strftime("%H:%M"),
                intent.device_id,
                intent.mode,
                (intent.planned_kw or 0.0) * 1000.0,
                intent.min_power_w or 0.0,
                intent.max_power_w or 0.0,
            )

    # ------------------------------------------------------------------
    # Contributor registry
    # ------------------------------------------------------------------

    def register_contributor(self, contributor: ControlContributor) -> None:
        """Add a contributor to the control loop.

        Contributors are processed in registration order.  For devices
        that share a common constraint (e.g. multiple batteries competing
        for the same grid connection), register them together and let the
        individual contributors arbitrate via their ``desired_setpoint_w``
        implementations.
        """
        self._contributors.append(contributor)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def tick(
        self,
        live: LiveSituation,
        registry: "DeviceRegistry",
    ) -> None:
        """Execute one control tick.

        For each registered contributor:
        1. Resolve the active ``ControlIntent`` (most recent intent ≤ now).
        2. Ask the contributor for its desired setpoint.
        3. Send ``DeviceCommand(set_power_w=…)`` when the setpoint is not None.
        4. Update the ``BatteryCostLedger`` from the *actual* measured power
           in ``live.device_states`` (not from the setpoint sent).

        After all devices are processed, apply gradual spot-floor decay to
        every ``StorageControlContributor``'s ledger entry.

        Parameters
        ----------
        live:
            Current energy situation snapshot.
        registry:
            Device registry used to look up and command devices.
        """
        setpoints = {
            device_id: (setpoint_w, intent)
            for device_id, setpoint_w, _mode, intent in self._compute_setpoints(live)
        }

        for contributor in self._contributors:
            setpoint_w, intent = setpoints[contributor.device_id]

            if setpoint_w is not None:
                device = registry.get(contributor.device_id)
                if device is not None:
                    await device.send_command(
                        DeviceCommand(
                            device_id=contributor.device_id,
                            command="set_power_w",
                            value=round(setpoint_w, 1),
                        )
                    )
                else:
                    _log.warning(
                        "ControlLoop: device %r not found in registry — skipping",
                        contributor.device_id,
                    )

            # Update ledger from *actual* measured power, not from setpoint.
            # This correctly accounts for partial delivery, SoC clamping, etc.
            state = live.device_states.get(contributor.device_id)
            if state is not None and state.power_w is not None and live.dt_hours > 0:
                actual_kwh = state.power_w / 1000.0 * live.dt_hours
                if actual_kwh > 0.0:
                    price = contributor.charge_price_eur_per_kwh(intent, live)
                    eta_c = (
                        contributor._constraints.charge_efficiency
                        if isinstance(contributor, StorageControlContributor)
                        else 0.95
                    )
                    self._ledger.record_charge(
                        contributor.device_id,
                        delta_kwh=actual_kwh,
                        price_eur_per_kwh=price,
                        charge_efficiency=eta_c,
                    )
                elif actual_kwh < 0.0:
                    self._ledger.record_discharge(
                        contributor.device_id,
                        delta_kwh=abs(actual_kwh),
                    )

        # Gradual spot-floor decay for all storage contributors.
        for contributor in self._contributors:
            if isinstance(contributor, StorageControlContributor):
                self._ledger.apply_spot_floor(
                    contributor.device_id,
                    spot_price=live.current_price_eur_per_kwh,
                    dt_hours=live.dt_hours,
                    max_charge_kw=contributor._constraints.max_charge_kw,
                    charge_efficiency=contributor._constraints.charge_efficiency,
                )

    # ------------------------------------------------------------------
    # Dry-run helper
    # ------------------------------------------------------------------

    def describe_setpoints(
        self,
        live: LiveSituation,
    ) -> list[tuple[str, float | None, str]]:
        """Compute what ``tick()`` *would* send without touching any device.

        Returns a list of ``(device_id, setpoint_w, mode)`` tuples — one per
        registered contributor.  Discharge setpoints are capped to the
        current grid import demand (same logic as ``tick()``).
        """
        return [
            (device_id, setpoint_w, mode)
            for device_id, setpoint_w, mode, _intent in self._compute_setpoints(live)
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_setpoints(
        self,
        live: LiveSituation,
    ) -> list[tuple[str, float | None, str, ControlIntent | None]]:
        """Resolve setpoints for all contributors.

        First tries a one-step realtime MILP for storage contributors using
        current live conditions plus the active plan slice. If that fails,
        falls back to the legacy sequential capping logic.

        Returns ``(device_id, setpoint_w, mode, intent)`` for every contributor.
        """
        intents: dict[str, ControlIntent | None] = {
            c.device_id: self._find_intent(c.device_id, live.timestamp)
            for c in self._contributors
        }
        grid_ref_w = (
            live.default_zone_grid_power_w
            if live.default_zone_grid_power_w is not None
            else live.grid_power_w
        )
        # Recover controllable demand headroom by adding battery discharge that
        # is already happening right now; otherwise a discharge command can
        # self-cancel to zero on the next tick when grid import briefly hits 0.
        current_discharge_w = sum(
            -live.device_states[c.device_id].power_w
            for c in self._contributors
            if c.device_id in live.device_states
            and live.device_states[c.device_id].power_w is not None
            and live.device_states[c.device_id].power_w < 0
        )
        effective_grid_w = grid_ref_w + current_discharge_w

        storage_inputs: list[StorageSliceInput] = []
        for contributor in self._contributors:
            if not isinstance(contributor, StorageControlContributor):
                continue
            intent = intents[contributor.device_id]
            planned_w = 0.0
            if intent is not None:
                if intent.planned_kw is not None:
                    planned_w = intent.planned_kw * 1000.0
                elif intent.mode == "grid_fill":
                    planned_w = max(0.0, intent.max_power_w or 0.0)
                elif intent.mode == "discharge":
                    planned_w = min(0.0, intent.min_power_w or 0.0)

                # Soften discharge tracking when live PV surplus exists so the
                # realtime layer may absorb otherwise-exported PV.
                if intent.mode == "discharge" and live.grid_power_w < -1.0:
                    planned_w = min(planned_w, 0.0)
                    planned_w = 0.0
            storage_inputs.append(
                StorageSliceInput(
                    device_id=contributor.device_id,
                    max_charge_w=contributor._constraints.max_charge_kw * 1000.0,
                    max_discharge_w=contributor._constraints.max_discharge_kw * 1000.0,
                    no_grid_charge=contributor._constraints.no_grid_charge,
                    mode=intent.mode if intent is not None else "no_plan",
                    planned_w=planned_w,
                    reserved_kwh=(intent.reserved_kwh if intent is not None and intent.reserved_kwh is not None else 0.0),
                    charge_policy=(intent.charge_policy if intent is not None else "auto") or "auto",
                    discharge_policy=(intent.discharge_policy if intent is not None else "meet_load_only") or "meet_load_only",
                    prev_setpoint_w=self._last_storage_setpoints_w.get(contributor.device_id, 0.0),
                )
            )

        optimized_storage = None
        if storage_inputs:
            optimized_storage = optimize_storage_slice(
                storage_inputs,
                grid_power_w=effective_grid_w,
                pv_surplus_w=max(0.0, -effective_grid_w),
                dt_hours=max(1e-6, live.dt_hours),
                import_price_eur_per_kwh=live.current_price_eur_per_kwh,
                export_price_eur_per_kwh=live.pv_opportunity_price_eur_per_kwh,
                cost_basis_eur_per_kwh=self._ledger.all_cost_bases(),
            )
            if optimized_storage is None:
                _log.warning("ControlLoop: realtime slice optimizer infeasible — using legacy capping")

        # Available headroom for battery discharge = grid import + whatever the
        # batteries are already contributing.  If the batteries are currently
        # supplying 1 kW and the heat pump is using 2 kW, grid import is only
        # 1 kW — but the house load is 3 kW, so we can safely discharge up to
        # 3 kW total without pushing anything back to the grid.
        remaining_import_w = max(0.0, grid_ref_w + current_discharge_w)

        result: list[tuple[str, float | None, str, ControlIntent | None]] = []
        for contributor in self._contributors:
            intent = intents[contributor.device_id]
            mode = intent.mode if intent is not None else "no_plan"

            if (
                optimized_storage is not None
                and isinstance(contributor, StorageControlContributor)
                and contributor.device_id in optimized_storage
            ):
                setpoint_w = optimized_storage[contributor.device_id]
                self._last_storage_setpoints_w[contributor.device_id] = setpoint_w
                result.append((contributor.device_id, setpoint_w, mode, intent))
                continue

            raw_w = contributor.desired_setpoint_w(intent, live)
            if raw_w is not None and raw_w < 0:
                discharge_policy = (
                    (intent.discharge_policy if intent is not None else "meet_load_only")
                    or "meet_load_only"
                )
                allow_export = self._allow_export(discharge_policy, contributor, live)
                if allow_export:
                    setpoint_w = raw_w
                else:
                    # Cap: don't discharge more than remaining grid import
                    capped = max(raw_w, -remaining_import_w)
                    if capped != raw_w:
                        _log.info(
                            "ControlLoop: discharge capped  %s  %.0f → %.0f W"
                            "  (grid_import=%.0f W)",
                            contributor.device_id, raw_w, capped, grid_ref_w,
                        )
                    remaining_import_w = max(0.0, remaining_import_w + capped)
                    setpoint_w = capped
            else:
                setpoint_w = raw_w

            if isinstance(contributor, StorageControlContributor) and setpoint_w is not None:
                self._last_storage_setpoints_w[contributor.device_id] = setpoint_w

            result.append((contributor.device_id, setpoint_w, mode, intent))
        return result

    def _allow_export(
        self,
        discharge_policy: str,
        contributor: ControlContributor,
        live: LiveSituation,
    ) -> bool:
        """Return True when this tick may export battery energy to the grid."""
        if discharge_policy in ("meet_load_only", "forbid_export", "auto"):
            return False
        if discharge_policy != "allow_export_if_profitable":
            return False

        basis = self._ledger.cost_basis(contributor.device_id)
        if basis is None:
            return False
        return basis <= live.pv_opportunity_price_eur_per_kwh

    def _find_intent(
        self,
        device_id: str,
        now: datetime,
    ) -> ControlIntent | None:
        """Return the active ``ControlIntent`` for *device_id* at *now*.

        "Active" means the intent with the *greatest* ``timestep`` that is
        still ≤ *now* (i.e. we are inside that planning slot).
        Returns ``None`` when no plan is active or no matching intent exists.
        """
        if self._active_plan is None:
            return None

        relevant = [
            intent
            for intent in self._active_plan.intents
            if intent.device_id == device_id and intent.timestep <= now
        ]
        if not relevant:
            return None
        return max(relevant, key=lambda i: i.timestep)

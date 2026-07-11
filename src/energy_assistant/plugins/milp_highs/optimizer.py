"""MilpHigsOptimizer — cost-minimising MILP optimizer using the HiGHS solver.

Algorithm overview
------------------
Time is discretised into fixed-length steps of ``step_minutes`` (default 60).
For each storage device *b* (taken from ``context.storage_constraints``) and
each time step *t* the model introduces four groups of decision variables:

    c[b,t]  — AC energy charged into the battery   (Wh, ≥ 0)
    d[b,t]  — AC energy discharged from the battery (Wh, ≥ 0)
    u[b,t]  — binary: 1 = charging, 0 = discharging/idle
    e[b,t]  — stored energy at END of step *t*       (Wh)

All LP variables use Wh (not kWh) for better numerical conditioning: whole-watt
power values produce exact integer Wh coefficients (e.g. 4140 W × 0.25 h = 1035 Wh
exactly, vs 4.14 kW × 0.25 h = 1.0350000000000001 kWh with float rounding).
Prices are scaled to €/Wh internally. Public APIs (ControlIntent, StorageConstraints)
still use kW and kWh; conversion happens at the _build_model boundary.

Grid energy consumed per step (positive = import, negative = export):

    g[t] = net_load[t] + Σ_b c[b,t] − Σ_b d[b,t]

SoC dynamics (with charge efficiency ηc and discharge efficiency ηd):

    e[b,t] = e[b,t−1] + ηc · c[b,t] − d[b,t] / ηd

Grid energy split into import and export (both ≥ 0):

    g_imp[t]  — energy drawn from the grid  (Wh, ≥ 0)
    g_exp[t]  — energy fed into the grid    (Wh, ≥ 0)
    g_imp[t] − g_exp[t] = net_load[t] + Σ_b c[b,t] − Σ_b d[b,t]

Objective (minimise net electricity cost over the horizon):

    min Σ_t [ import_price[t] · g_imp[t] − export_price[t] · g_exp[t] ]

Because ``import_price[t] > export_price[t] ≥ 0`` always holds in practice,
the solver will never simultaneously import and export in the same step
(no extra binary variable is needed to prevent this).

The export price is resolved from the tariff in ``context.tariffs`` that
has a non-zero ``export_price_schedule`` — typically the ``grid`` tariff.

Inputs from OptimizationContext
 --------------------------------
* ``storage_constraints`` — physical limits of every storage device.
* ``device_states``        — initial SoC (``soc_pct``) per device.
* ``forecasts``            — ``ForecastQuantity.PRICE``,
                             ``ForecastQuantity.PV_GENERATION``,
                             ``ForecastQuantity.CONSUMPTION`` (all in kW).
* ``tariffs``              — used as a fallback for prices when no PRICE
                             forecast is present.
* ``horizon``              — planning window (default 24 h).

Output
------
An ``EnergyPlan`` whose ``intents`` contain one ``ControlIntent`` per
(device, timestep) pair.  Modes used:

* ``"charge_from_pv"``   — absorb PV surplus only; never increase grid import.
* ``"charge_from_grid"`` — charge at planned power; grid import is allowed.
* ``"discharge"``        — reduce import at site level; no export crossing.
* ``"grid_feed_in"``     — actively push stored energy into the grid.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pulp

from ...assets.ev import EvChargingGoal
from ...core.models import (
    ControlIntent,
    EnergyPlan,
    PlanFlow,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
    TariffPoint,
    ThresholdConstraints,
)
from ...core.optimizer import OptimizationContext

_log = logging.getLogger(__name__)

# Default electricity price used when no tariff or forecast is available.
_DEFAULT_PRICE_EUR_KWH = 0.30

# Internal LP unit scale: all energy variables are in Wh, not kWh.
# Prices are divided by this factor (€/kWh → €/Wh); energies are multiplied
# by it (kWh → Wh) at the _build_model boundary and divided back in the
# extract functions. Using Wh lets whole-watt power values produce exact
# integer energy coefficients, improving LP numerical conditioning.
_WH = 1000.0


class MilpHigsOptimizer:
    """Cost-minimising MILP optimizer backed by the HiGHS solver (via PuLP).

    Parameters
    ----------
    step_minutes:
        Duration of each planning time step in minutes.  Must be a divisor
        of 60 or a multiple of 60.  Default is 60 (one-hour steps).
    precision:
        Speed/accuracy trade-off in [0, 1], controlling two solver knobs:

        * MIP gap tolerance, log-linear: 0.0 → 1e-2 (may leave ~a cent on
          the table), 0.5 → ~3e-6, 1.0 → 1e-9 (nano-euro exact).
        * Solve time limit: 5 s at 0.0 rising linearly to 60 s at ~1.0;
          exactly 1.0 disables the limit. When the limit is reached HiGHS
          returns the best plan found so far (still typically within a few
          cents of optimal — good incumbents are found in the first
          seconds; the long tail is spent *proving* optimality against
          the tiny battery tie-break terms).

        Below ~0.45 the gap exceeds the ~1e-4 € tie-break terms, so
        battery dispatch *ordering* may become arbitrary among cost-equal
        plans; plan economics are unaffected beyond the gap itself.
    """

    def __init__(self, step_minutes: int = 60, precision: float = 0.5) -> None:
        self._step_min = step_minutes
        self._precision = min(max(float(precision), 0.0), 1.0)

    # ------------------------------------------------------------------
    # Public interface — Optimizer protocol
    # ------------------------------------------------------------------

    async def optimize(self, context: OptimizationContext) -> EnergyPlan:
        """Run the MILP optimisation and return the resulting EnergyPlan."""
        step_h = self._step_min / 60.0
        step_td = timedelta(minutes=self._step_min)
        horizon_h = int(context.horizon.total_seconds() / 3600)

        n_steps = max(0, int(context.horizon / step_td))
        if n_steps == 0:
            return EnergyPlan(horizon_hours=horizon_h, step_minutes=self._step_min)

        _now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Floor to the step boundary so timestamps align with price/PV forecasts
        now = _now - timedelta(minutes=_now.minute % self._step_min)
        timestamps = [now + step_td * t for t in range(n_steps)]

        # ── Prices ────────────────────────────────────────────────────
        prices = await self._resolve_prices(context, timestamps)
        export_prices = await self._resolve_export_prices(context, timestamps)

        # ── Net load (kWh per step) ────────────────────────────────────
        # net_load > 0  → home consumes from grid
        # net_load < 0  → PV surplus fed to grid (before battery action)
        consumption_kw = _interpolate_kw(
            context.forecasts.get(ForecastQuantity.CONSUMPTION, []), timestamps
        )
        pv_kw = _interpolate_kw(
            context.forecasts.get(ForecastQuantity.PV_GENERATION, []), timestamps
        )

        # ── Live-PV blend for the current hour ────────────────────────
        # Hourly PV forecasts (e.g. pvforecast iobroker) often return 0 or a
        # stale value for the current clock-hour.  Anchor the current slot to
        # the live device reading (ground truth), then approach the forecast
        # linearly so the series lands on the forecast value at the end of
        # the hour — correcting both under- and over-forecasts without
        # distorting anything beyond the current hour.
        storage_ids = {sc.device_id for sc in context.storage_constraints}
        ev_ids = {g.device_id for g in context.ev_charging_goals}
        producer_ids = context.producer_device_ids
        live_pv_kw = sum(
            abs(state.power_w) / 1000.0
            for device_id, state in context.device_states.items()
            if state.power_w is not None
            and state.power_w < -100          # meaningful production (>100 W)
            and device_id not in storage_ids
            and device_id not in ev_ids
            # When the context knows which devices are PV producers, restrict
            # to those only — bidirectional meters also report negative power
            # when the site is exporting, which would falsely inflate the floor.
            and (not producer_ids or device_id in producer_ids)
        )
        if live_pv_kw > 0.1:
            current_hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            # Forecast value we blend toward: the first slot at/after hour end.
            fc_end = next(
                (p for ts, p in zip(timestamps, pv_kw) if ts >= current_hour_end),
                None,
            )
            span_s = max(1.0, (current_hour_end - now).total_seconds())
            blended: list[float] = []
            for ts, p in zip(timestamps, pv_kw):
                if ts >= current_hour_end:
                    blended.append(p)
                elif fc_end is None:
                    blended.append(max(p, live_pv_kw))  # no anchor — legacy floor
                else:
                    frac = min(1.0, max(0.0, (ts - now).total_seconds() / span_s))
                    blended.append(live_pv_kw * (1.0 - frac) + fc_end * frac)
            pv_kw = blended

        net_load = [(c - p) * step_h for c, p in zip(consumption_kw, pv_kw)]
        # Keep a copy of the baseline net_load for EV mode classification later.
        baseline_net_load = list(net_load)

        # ── EV phase-2 top-off: add mandatory instant-charge load ─────
        # Phase 2 (charge_limit → target_soc) is scheduled just before the
        # deadline at max_charge_kw.  Adding it to net_load lets the MILP
        # plan house batteries and grid around this known demand.
        ev_goals: list[EvChargingGoal] = context.ev_charging_goals
        for goal in ev_goals:
            if not goal.connected or goal.phase2_required_kwh <= 0.01:
                continue
            for t, ts in enumerate(timestamps):
                if goal.phase2_start_time <= ts < goal.target_by:
                    net_load[t] += goal.max_charge_kw * step_h

        # ── Storage devices ────────────────────────────────────────────
        batteries = context.storage_constraints
        if not batteries:
            _log.info("MilpHigsOptimizer: no storage constraints — returning empty plan")
            return EnergyPlan(horizon_hours=horizon_h, step_minutes=self._step_min)

        initial_energy = self._initial_energy(batteries, context)

        # ── Terminal value: max(actual cost basis, p70 × η_d) ───────────
        # Reflects what 1 kWh stored in the battery is worth at the end of
        # the planning horizon.  Two competing lower bounds:
        #
        #   cost_basis  — what we actually paid per stored kWh (ledger).
        #                 Sets the economic floor: never sell below cost.
        #                 Discharge threshold = cost_basis / η_d.
        #
        #   p70 × (η_d − 0.01)  — expected future dispatch value based on
        #                 forecast prices.  p70 = 70th-percentile of the
        #                 48-hour price curve.  The (−0.01) offset ensures
        #                 discharge is triggered AT p70, not just above it
        #                 (avoids the exact-threshold numerical tie).
        #                 Discharge threshold ≈ p70.
        #
        # Taking max() means: the optimizer holds unless BOTH the price is
        # high enough to cover cost AND it's in the top ~30% of prices.
        # When basis = 0.0 (first start), only the p70 term applies.
        # Once the ledger has tracked real charge prices, the basis term
        # prevents selling below cost regardless of the price distribution.
        sorted_prices = sorted(prices)
        p30 = sorted_prices[int(0.30 * len(sorted_prices))]
        p70 = sorted_prices[int(0.70 * len(sorted_prices))]
        cost_bases = context.battery_cost_basis or {}
        terminal_value_basis: dict[str, float] = {}
        for sc in batteries:
            if sc.no_grid_charge:
                # PV-only battery: recharges for free from PV every day.
                # Using p70 over-values stored energy and blocks discharge.
                # Using export_price under-values it: the battery discharges at
                # any positive price, depleting itself during cheap daytime EV
                # slots and leaving nothing for overnight — and won't plan PV
                # recharging (TV < export + degradation/η_c).
                #
                # p30 × (η_d − 0.01) sits between the two: discharge threshold
                # ≈ p30 (below typical night prices, above typical daytime).
                # PV recharging is profitable because TV × η_c > export + deg.
                tv = max(
                    cost_bases.get(sc.device_id, 0.0),
                    p30 * max(0.0, sc.discharge_efficiency - 0.01),
                )
            else:
                tv = max(
                    cost_bases.get(sc.device_id, 0.0),
                    p70 * max(0.0, sc.discharge_efficiency - 0.01),
                )
            terminal_value_basis[sc.device_id] = tv
        _log.debug(
            "MilpHigsOptimizer: p30=%.4f  p70=%.4f €/kWh  TV=%s  (from %d price steps)",
            p30,
            p70,
            {sc.device_id: round(terminal_value_basis[sc.device_id], 4) for sc in batteries},
            len(prices),
        )

        # ── Threshold devices ──────────────────────────────────────────
        threshold_devices = context.threshold_constraints
        initial_threshold_values = self._initial_threshold_values(threshold_devices, context)
        initial_threshold_running = self._initial_threshold_running(threshold_devices, context)

        # ── Build and solve the MILP model ─────────────────────────────
        prob, variables = self._build_model(
            n_steps, step_h, batteries, net_load, prices, export_prices,
            initial_energy, context.battery_cost_basis, terminal_value_basis,
            ev_goals=ev_goals, timestamps=timestamps,
            threshold_devices=threshold_devices,
            initial_threshold_values=initial_threshold_values,
            initial_threshold_running=initial_threshold_running,
        )
        t0 = time.monotonic()
        # Solve in a worker thread: prob.solve() blocks for up to the full
        # time limit, and running it on the event loop would freeze every
        # API endpoint (and the control loop) for the duration.
        status = await asyncio.to_thread(prob.solve, self._get_solver())
        _log.info(
            "MilpHigsOptimizer: solved in %.2fs — %d steps, %d batteries, %d EVs, status=%s",
            time.monotonic() - t0,
            n_steps,
            len(batteries),
            len([g for g in (ev_goals or []) if g.connected]),
            pulp.LpStatus[status],
        )

        if pulp.LpStatus[status] not in ("Optimal", "Feasible"):
            _log.warning(
                "MilpHigsOptimizer: solver returned %r — emitting empty plan",
                pulp.LpStatus[status],
            )
            return EnergyPlan(horizon_hours=horizon_h, step_minutes=self._step_min)

        # ── Extract schedule → EnergyPlan ─────────────────────────────
        intents = _extract_intents(batteries, variables, timestamps, step_h)
        ev_intents = _extract_ev_intents(
            ev_goals, variables, timestamps, step_h
        )
        # Phase-2 intents (mandatory top-off slots, handled outside the MILP)
        for goal in ev_goals:
            if not goal.connected or goal.phase2_required_kwh <= 0.01:
                continue
            for t, ts in enumerate(timestamps):
                if goal.phase2_start_time <= ts < goal.target_by:
                    ev_intents.append(ControlIntent(
                        device_id=goal.device_id,
                        timestep=ts,
                        power_kw=round(goal.max_charge_kw, 4),
                        grid_allowed=True,
                        reserved_kwh=round(goal.max_charge_kw * step_h, 4),
                    ))
        threshold_intents = _extract_threshold_intents(threshold_devices, variables, timestamps)

        # Solved site-level flows per step — includes internal adjustments
        # (live-PV floor, phase-2 loads) so the UI shows grid import/export
        # consistent with the plan instead of re-deriving from raw forecasts.
        g_imp_v = variables["g_imp"]
        g_exp_v = variables["g_exp"]
        flows = [
            PlanFlow(
                timestep=ts,
                pv_kw=round(pv_kw[t], 4),
                grid_import_kw=round((pulp.value(g_imp_v[t]) or 0.0) / _WH / step_h, 4),
                grid_export_kw=round((pulp.value(g_exp_v[t]) or 0.0) / _WH / step_h, 4),
            )
            for t, ts in enumerate(timestamps)
        ]

        return EnergyPlan(
            horizon_hours=horizon_h,
            step_minutes=self._step_min,
            intents=intents + ev_intents + threshold_intents,
            flows=flows,
        )

    # ------------------------------------------------------------------
    # Model construction
    # ------------------------------------------------------------------

    def _build_model(
        self,
        n_steps: int,
        step_h: float,
        batteries: list[StorageConstraints],
        net_load: list[float],
        prices: list[float],
        export_prices: list[float],
        initial_energy: dict[str, float],
        battery_cost_basis: dict[str, float] | None = None,
        terminal_value_basis: dict[str, float] | None = None,
        ev_goals: list[EvChargingGoal] | None = None,
        timestamps: list[datetime] | None = None,
        threshold_devices: list[ThresholdConstraints] | None = None,
        initial_threshold_values: dict[str, float] | None = None,
        initial_threshold_running: dict[str, bool] | None = None,
    ) -> tuple[pulp.LpProblem, dict]:
        """Construct the PuLP problem and return (problem, variables dict).

        Parameters
        ----------
        battery_cost_basis:
            Cost basis (€/kWh) for energy *already stored* — used as the
            discharge threshold (the optimizer won't discharge below this).
        terminal_value_basis:
            Expected future market value (€/kWh) of energy left in the
            battery at the END of the horizon.  This is the key knob for
            PV recharge incentives: charging from PV is worthwhile when
            terminal_value_basis > export_price/η_c + degradation_cost.
            If None, falls back to battery_cost_basis.
        """
        prob = pulp.LpProblem("energy_cost_optimizer", pulp.LpMinimize)
        T = range(n_steps)

        # ── Input boundary: kW → W, kWh → Wh, €/kWh → €/Wh ─────────────
        # Everything inside the model uses W (power) and Wh (energy).
        # Inputs are converted here once; extract functions convert back.
        net_load_wh = [nl * _WH for nl in net_load]
        p_imp = [p / _WH for p in prices]          # €/kWh → €/Wh
        p_exp = [p / _WH for p in export_prices]   # €/kWh → €/Wh

        active_ev_goals = [g for g in (ev_goals or []) if g.connected]
        active_threshold_devices = list(threshold_devices or [])
        initial_vals = initial_threshold_values or {}
        initial_running = initial_threshold_running or {}
        ts_list = timestamps or []

        # Per-battery W / Wh limits (power_w × step_h = energy_wh)
        bat_max_charge_w    = {sc.device_id: sc.max_charge_kw    * _WH for sc in batteries}
        bat_max_discharge_w = {sc.device_id: sc.max_discharge_kw * _WH for sc in batteries}
        bat_e_min_wh        = {sc.device_id: sc.capacity_kwh * sc.min_soc_pct / 100.0 * _WH for sc in batteries}
        bat_e_max_wh        = {sc.device_id: sc.capacity_kwh * sc.max_soc_pct / 100.0 * _WH for sc in batteries}
        bat_e_init_wh       = {b: e * _WH for b, e in initial_energy.items()}

        # Per-EV W / Wh limits
        ev_min_w          = {g.asset_id: g.min_charge_kw      * _WH for g in active_ev_goals}
        ev_max_w          = {g.asset_id: g.max_charge_kw      * _WH for g in active_ev_goals}
        ev_phase1_req_wh  = {g.asset_id: g.phase1_required_kwh * _WH for g in active_ev_goals}

        # Per-threshold device W
        thresh_rated_w = {tc.device_id: tc.rated_power_kw * _WH for tc in active_threshold_devices}

        # ── Decision variables (all energy in Wh) ─────────────────────
        c: dict[tuple[str, int], pulp.LpVariable] = {}  # charge energy (Wh)
        d: dict[tuple[str, int], pulp.LpVariable] = {}  # discharge energy (Wh)
        u: dict[tuple[str, int], pulp.LpVariable] = {}  # binary: 1 = charging
        e: dict[tuple[str, int], pulp.LpVariable] = {}  # stored energy (Wh)

        for sc in batteries:
            b = sc.device_id
            for t in T:
                c[(b, t)] = pulp.LpVariable(f"c__{b}__{t}", lowBound=0)
                d[(b, t)] = pulp.LpVariable(f"d__{b}__{t}", lowBound=0)
                u[(b, t)] = pulp.LpVariable(f"u__{b}__{t}", cat="Binary")
                e[(b, t)] = pulp.LpVariable(f"e__{b}__{t}", lowBound=bat_e_min_wh[b], upBound=bat_e_max_wh[b])

        # ── EV charging variables (energy in Wh) ──────────────────────
        # ev[asset_id, t]      — AC energy charged into the EV in step t (Wh).
        # ev_on[asset_id, t]   — binary: 1 = charger is active this step.
        # ev_grid[asset_id, t] — binary: 1 = grid-sourced slot (executes as
        #                        Instant Charging); 0 = PV slot, where the EV
        #                        energy is capped to the forecast PV surplus
        #                        because execution (openWB PV mode) can only
        #                        track the real live surplus.
        #
        # Semi-continuous constraint: ev is either 0 (charger off) or in
        # [ev_min_w * step_h, ev_max_w * step_h] (charger on).
        # This prevents the optimizer from planning sub-minimum currents that
        # the charger cannot physically deliver (openWB requires ≥ 6 A).
        #
        # Phase-2 slots are excluded: they are fixed loads in net_load already.
        ev:      dict[tuple[str, int], pulp.LpVariable] = {}
        ev_on:   dict[tuple[str, int], pulp.LpVariable] = {}
        ev_grid: dict[tuple[str, int], pulp.LpVariable] = {}
        for goal in active_ev_goals:
            max_wh = ev_max_w[goal.asset_id] * step_h
            for t in T:
                ts = ts_list[t] if t < len(ts_list) else None
                key = (goal.asset_id, t)
                if ts is not None and ts < goal.phase2_start_time:
                    ev[key]    = pulp.LpVariable(f"ev__{goal.asset_id}__{t}",    lowBound=0, upBound=max_wh)
                    ev_on[key] = pulp.LpVariable(f"ev_on__{goal.asset_id}__{t}", cat="Binary")
                    # pv_only goals may never use the grid → fix the binary to 0.
                    ev_grid[key] = pulp.LpVariable(
                        f"ev_grid__{goal.asset_id}__{t}", cat="Binary",
                        upBound=0 if goal.pv_only else 1,
                    )
                else:
                    ev[key] = pulp.LpVariable(f"ev__{goal.asset_id}__{t}", lowBound=0, upBound=0.0)

        # ── Threshold device variables ─────────────────────────────────
        # run[device_id, t] — binary: 1 = device is running this step
        # val[device_id, t] — measured value at END of this step
        run: dict[tuple[str, int], pulp.LpVariable] = {}
        val: dict[tuple[str, int], pulp.LpVariable] = {}
        for tc in active_threshold_devices:
            td_id = tc.device_id
            for t in T:
                run[(td_id, t)] = pulp.LpVariable(f"run__{td_id}__{t}", cat="Binary")
                val[(td_id, t)] = pulp.LpVariable(
                    f"val__{td_id}__{t}",
                    lowBound=tc.bottom_threshold,
                    upBound=tc.top_threshold,
                )

        # Grid energy per step: split into import (≥0) and export (≥0) — Wh
        g_imp = {t: pulp.LpVariable(f"g_imp__{t}", lowBound=0) for t in T}
        g_exp = {t: pulp.LpVariable(f"g_exp__{t}", lowBound=0) for t in T}

        # ── Objective ─────────────────────────────────────────────────
        # Grid cost: p_imp/p_exp are €/Wh, g_imp/g_exp are Wh → product is €.
        grid_cost = pulp.lpSum(
            p_imp[t] * g_imp[t] - p_exp[t] * g_exp[t] for t in T
        )
        # Terminal value: energy remaining at end of horizon is worth
        # tv_basis €/kWh — subtract it (it reduces net cost).
        # tv_basis is SEPARATE from battery_cost_basis (the discharge threshold):
        # it should reflect the expected *future market value* of stored energy
        # so the optimizer is incentivised to refill from PV whenever future
        # dispatch prices exceed the effective charging cost.
        # e is in Wh, tv_basis in €/kWh → coefficient = tv_basis / _WH (€/Wh).
        tv_basis = terminal_value_basis if terminal_value_basis is not None else (battery_cost_basis or {})
        terminal_value = pulp.lpSum(
            tv_basis.get(sc.device_id, 0.0) / _WH * e[(sc.device_id, n_steps - 1)]
            for sc in batteries
        )
        # Degradation cost: each Wh stored costs degradation_cost_per_kwh / _WH per Wh.
        # Applied to η_c × c[b,t] (the Wh actually stored per AC Wh charged).
        degradation_cost = pulp.lpSum(
            sc.degradation_cost_per_kwh / _WH * sc.charge_efficiency * c[(sc.device_id, t)]
            for sc in batteries
            for t in T
        )
        # Priority-dispatch tiebreak: when batteries are economically degenerate
        # (swapping which battery fires at which step leaves the total objective
        # unchanged, because Σ deg×d is order-invariant), we need a term that IS
        # sensitive to ordering.
        #
        # Solution: penalise the time-integral of the priority battery's SoC:
        #   +ε × Σ_t  e[priority, t]
        # Minimising this forces the priority battery (lowest degradation cost)
        # to hold as little energy as possible across the horizon, i.e. it
        # discharges first and fastest.
        #
        # Scale ε = 1e-5 / _WH = 1e-8 (e now in Wh, not kWh):
        #   • Benefit of one step of early Zendure discharge:
        #       1e-8 × (0.175/0.95) × 1000 Wh × 48  ≈  8.8e-5 € — above gapAbs=1e-9.
        #   • Well below price economics (~1e-3 €/step) → no real decisions changed.
        sorted_by_deg = sorted(batteries, key=lambda sc: sc.degradation_cost_per_kwh)
        if len(sorted_by_deg) >= 2:
            pb = sorted_by_deg[0].device_id   # cheapest-to-wear battery
            priority_tiebreak = 1e-5 / _WH * pulp.lpSum(e[(pb, t)] for t in T)
        else:
            priority_tiebreak = 0

        # PV-first charging tiebreak for mixed battery capabilities:
        # when there is PV surplus in a step and at least one battery is
        # ``no_grid_charge`` (e.g. SMA) while others may charge from grid
        # (e.g. Zendure), prefer allocating that surplus to the constrained
        # batteries first. This keeps flexible batteries available for later
        # grid charging windows.
        # Scale 1e-4 / _WH = 1e-7 (c now in Wh, not kWh).
        no_grid_ids = [sc.device_id for sc in batteries if sc.no_grid_charge]
        flexible_ids = [sc.device_id for sc in batteries if not sc.no_grid_charge]
        if no_grid_ids and flexible_ids:
            pv_priority_tiebreak = 1e-4 / _WH * pulp.lpSum(
                pulp.lpSum(c[(b, t)] for b in flexible_ids)
                - pulp.lpSum(c[(b, t)] for b in no_grid_ids)
                for t in T
                if net_load_wh[t] < 0.0
            )
        else:
            pv_priority_tiebreak = 0

        # Tiny penalty on grid-sourced EV slots so the solver labels a slot
        # PV whenever the surplus cap alone would satisfy it (without this,
        # ev_grid=1 is free and labels become arbitrary when ev ≤ surplus).
        ev_grid_tiebreak = 1e-3 * pulp.lpSum(ev_grid.values()) if ev_grid else 0

        prob += (
            grid_cost
            + degradation_cost
            + priority_tiebreak
            + pv_priority_tiebreak
            + ev_grid_tiebreak
            - terminal_value
        ), "total_cost"

        # ── Constraints ───────────────────────────────────────────────
        for t in T:
            # Grid energy balance: import − export = net demand (all Wh)
            prob += (
                g_imp[t] - g_exp[t]
                == net_load_wh[t]
                + pulp.lpSum(c[(sc.device_id, t)] for sc in batteries)
                - pulp.lpSum(d[(sc.device_id, t)] for sc in batteries)
                + pulp.lpSum(ev[(goal.asset_id, t)] for goal in active_ev_goals)
                + pulp.lpSum(
                    run[(tc.device_id, t)] * thresh_rated_w[tc.device_id] * step_h
                    for tc in active_threshold_devices
                ),
                f"grid_balance__{t}",
            )

        # ── EV semi-continuous constraints (min current) ───────────────
        # ev is either 0 or ≥ ev_min_w * step_h (no sub-minimum charging).
        for goal in active_ev_goals:
            min_wh = ev_min_w[goal.asset_id] * step_h
            max_wh = ev_max_w[goal.asset_id] * step_h
            for t in T:
                key = (goal.asset_id, t)
                if key not in ev_on:
                    continue
                prob += ev[key] >= min_wh * ev_on[key], f"ev_min__{goal.asset_id}__{t}"
                prob += ev[key] <= max_wh * ev_on[key], f"ev_max__{goal.asset_id}__{t}"

        # ── EV source constraint: PV slot ⇒ capped to forecast surplus ─
        # A slot is either grid-sourced (ev_grid=1 → Instant Charging, power
        # up to max) or PV-sourced (ev_grid=0 → openWB PV mode).  PV mode can
        # only track the real live surplus, so the plan must not assign more
        # than the forecast surplus — otherwise the planned energy silently
        # never materialises (and battery discharge would appear to "fill"
        # PV slots, mislabelling grid/battery energy as PV in the UI).
        for goal in active_ev_goals:
            max_wh = ev_max_w[goal.asset_id] * step_h
            for t in T:
                key = (goal.asset_id, t)
                if key not in ev_grid:
                    continue
                pv_surplus_wh = max(0.0, -net_load_wh[t])
                prob += (
                    ev[key] <= pv_surplus_wh + max_wh * ev_grid[key],
                    f"ev_pv_surplus__{goal.asset_id}__{t}",
                )

        # ── EV phase-1 deadline constraints ───────────────────────────
        for goal in active_ev_goals:
            if ev_phase1_req_wh[goal.asset_id] <= 10.0:   # < 10 Wh ≈ negligible
                continue
            phase1_slots = [
                t for t in T
                if t < len(ts_list) and ts_list[t] < goal.phase2_start_time
            ]
            if not phase1_slots:
                continue
            prob += (
                pulp.lpSum(ev[(goal.asset_id, t)] for t in phase1_slots)
                >= ev_phase1_req_wh[goal.asset_id],
                f"ev_phase1_deadline__{goal.asset_id}",
            )

        # Tight per-step big-M (Wh): max possible grid import = consumption surplus
        # + max charge capacity of all batteries + max charge capacity of all EVs.
        # Used for the no_grid_charge constraints below.
        big_m = [
            max(0.0, net_load_wh[t])
            + sum(bat_max_charge_w[s.device_id] * step_h for s in batteries)
            + sum(ev_max_w[g.asset_id] * step_h for g in active_ev_goals)
            for t in T
        ]

        for sc in batteries:
            b = sc.device_id
            c_max_wh = bat_max_charge_w[b] * step_h
            d_max_wh = bat_max_discharge_w[b] * step_h
            eta_c = sc.charge_efficiency
            eta_d = sc.discharge_efficiency
            e_init_wh = bat_e_init_wh[b]

            for t in T:
                # Charge only when u=1; discharge only when u=0
                prob += c[(b, t)] <= c_max_wh * u[(b, t)], f"c_max__{b}__{t}"
                prob += d[(b, t)] <= d_max_wh * (1 - u[(b, t)]), f"d_max__{b}__{t}"
                # PV-only charging: when this battery is charging (u=1), grid import must
                # be zero.  Via the energy balance this automatically caps total battery
                # charging to the available PV surplus and correctly accounts for all other
                # batteries competing for that surplus — unlike a static cap on c[b,t].
                # At night (u=0 because no economic incentive) the constraint is inactive,
                # so other batteries may still charge from the grid freely.
                if sc.no_grid_charge:
                    prob += g_imp[t] <= big_m[t] * (1 - u[(b, t)]), f"no_grid_charge__{b}__{t}"

                # SoC dynamics (all in Wh; efficiency η is dimensionless — unchanged)
                e_prev = e_init_wh if t == 0 else e[(b, t - 1)]
                prob += (
                    e[(b, t)] == e_prev + eta_c * c[(b, t)] - d[(b, t)] / eta_d,
                    f"soc__{b}__{t}",
                )

        # ── EV PV-only constraint ──────────────────────────────────────
        # For pv_only goals (no schedule): EV may not increase grid import.
        # Same big-M approach as battery no_grid_charge.
        for goal in active_ev_goals:
            if not goal.pv_only:
                continue
            for t in T:
                key = (goal.asset_id, t)
                if key not in ev_on:
                    continue
                prob += g_imp[t] <= big_m[t] * (1 - ev_on[key]), f"no_grid_charge_ev__{goal.asset_id}__{t}"

        # ── Threshold device value dynamics ───────────────────────────
        # v[d, t] = v[d, t-1]  ± drift × dt  ∓ (active + drift) × dt × run[d, t]
        #
        # For "reduces" (cooler/dehumidifier): value drifts UP when off,
        #   drops DOWN when running.
        # For "increases" (heater/humidifier): value drifts DOWN when off,
        #   rises UP when running.
        for tc in active_threshold_devices:
            td_id = tc.device_id
            v_init = initial_vals.get(td_id, (tc.bottom_threshold + tc.top_threshold) / 2.0)
            combined_rate = (tc.active_rate_per_h + tc.drift_rate_per_h) * step_h
            for t in T:
                v_prev = v_init if t == 0 else val[(td_id, t - 1)]
                if tc.direction == "reduces":
                    # off: +drift, on: +drift − combined = −active
                    prob += (
                        val[(td_id, t)] == v_prev
                        + tc.drift_rate_per_h * step_h
                        - combined_rate * run[(td_id, t)],
                        f"val_dynamics__{td_id}__{t}",
                    )
                else:  # "increases"
                    # off: −drift, on: −drift + combined = +active
                    prob += (
                        val[(td_id, t)] == v_prev
                        - tc.drift_rate_per_h * step_h
                        + combined_rate * run[(td_id, t)],
                        f"val_dynamics__{td_id}__{t}",
                    )

        # ── Threshold min-runtime / min-offtime ────────────────────────
        # Compressor protection at the plan level (mirrors the live
        # ``ThresholdControlContributor`` safeguards): a start at step t
        # forces run=1 for ceil(min_runtime_h / step_h) steps, a stop
        # forces run=0 for ceil(min_offtime_h / step_h) steps.
        #
        # Standard min up/down-time formulation on the run[] binaries:
        #   start at t (run[t] − run[t−1] = 1) ⇒ run[t+k] ≥ 1  for k < up
        #   stop  at t (run[t−1] − run[t] = 1) ⇒ run[t+k] ≤ 0  for k < down
        # The t=0 transition uses the device's live on/off state, so a
        # plan-start blip is caught too. How long the device has already
        # been on/off is unknown here; the contributor covers that window.
        for tc in active_threshold_devices:
            td_id = tc.device_id
            up_steps = math.ceil(round(tc.min_runtime_h / step_h, 6))
            down_steps = math.ceil(round(tc.min_offtime_h / step_h, 6))
            if up_steps <= 1 and down_steps <= 1:
                continue
            run_init = 1.0 if initial_running.get(td_id) else 0.0
            for t in T:
                r_prev = run_init if t == 0 else run[(td_id, t - 1)]
                for k in range(1, up_steps):
                    if t + k >= n_steps:
                        break
                    prob += (
                        run[(td_id, t + k)] >= run[(td_id, t)] - r_prev,
                        f"min_runtime__{td_id}__{t}__{k}",
                    )
                for k in range(1, down_steps):
                    if t + k >= n_steps:
                        break
                    prob += (
                        run[(td_id, t + k)] <= 1 - r_prev + run[(td_id, t)],
                        f"min_offtime__{td_id}__{t}__{k}",
                    )

        variables = {"c": c, "d": d, "u": u, "e": e, "g_imp": g_imp, "g_exp": g_exp, "ev": ev, "ev_on": ev_on, "ev_grid": ev_grid, "run": run, "val": val}
        return prob, variables

    # ------------------------------------------------------------------
    # Price resolution
    # ------------------------------------------------------------------

    async def _resolve_export_prices(
        self,
        context: OptimizationContext,
        timestamps: list[datetime],
    ) -> list[float]:
        """Return an export (feed-in) price (€/kWh) for every timestamp.

        Scans all tariffs in the context for one with a non-zero
        ``export_price_schedule``.  Typically this is the ``grid`` tariff
        configured with ``export_price_eur_per_kwh``.  Falls back to 0.0
        (no export revenue) when no matching tariff is found.
        """
        for tariff in context.tariffs.values():
            try:
                sched: list[TariffPoint] = await tariff.export_price_schedule(context.horizon)
                if sched and any(tp.price_eur_per_kwh > 0.001 for tp in sched):
                    points = [
                        ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh)
                        for tp in sched
                    ]
                    _log.debug(
                        "MilpHigsOptimizer: using tariff %r for export prices",
                        tariff.tariff_id,
                    )
                    return _interpolate_kw(points, timestamps)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "MilpHigsOptimizer: export tariff %r failed: %s",
                    tariff.tariff_id,
                    exc,
                )
        return [0.0] * len(timestamps)

    async def _resolve_prices(
        self,
        context: OptimizationContext,
        timestamps: list[datetime],
    ) -> list[float]:
        """Return a price (€/kWh) for every timestamp.

        Priority:
        1. ``ForecastQuantity.PRICE`` forecast points in the context.
        2. First tariff in ``context.tariffs`` via ``price_schedule``.
        3. Hard-coded default (``_DEFAULT_PRICE_EUR_KWH``).
        """
        price_fc = context.forecasts.get(ForecastQuantity.PRICE, [])
        if price_fc:
            return _interpolate_kw(price_fc, timestamps)

        for tariff in context.tariffs.values():
            try:
                sched: list[TariffPoint] = await tariff.price_schedule(context.horizon)
                if not sched or not any(tp.price_eur_per_kwh > 0.001 for tp in sched):
                    _log.debug(
                        "MilpHigsOptimizer: tariff %r has no non-zero import prices — skipping",
                        tariff.tariff_id,
                    )
                    continue
                points = [
                    ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh)
                    for tp in sched
                ]
                _log.debug(
                    "MilpHigsOptimizer: using tariff %r for prices", tariff.tariff_id
                )
                return _interpolate_kw(points, timestamps)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "MilpHigsOptimizer: tariff %r failed: %s", tariff.tariff_id, exc
                )

        _log.warning(
            "MilpHigsOptimizer: no price data available — using default %.2f €/kWh",
            _DEFAULT_PRICE_EUR_KWH,
        )
        return [_DEFAULT_PRICE_EUR_KWH] * len(timestamps)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _initial_energy(
        batteries: list[StorageConstraints],
        context: OptimizationContext,
    ) -> dict[str, float]:
        """Return initial stored energy (kWh) keyed by device_id."""
        result: dict[str, float] = {}
        for sc in batteries:
            e_min = sc.capacity_kwh * sc.min_soc_pct / 100.0
            e_max = sc.capacity_kwh * sc.max_soc_pct / 100.0
            state = context.device_states.get(sc.device_id)
            if state is not None and state.soc_pct is not None:
                raw_energy = sc.capacity_kwh * state.soc_pct / 100.0
                result[sc.device_id] = max(e_min, min(e_max, raw_energy))
            else:
                default_soc_pct = (sc.min_soc_pct + sc.max_soc_pct) / 2.0
                result[sc.device_id] = sc.capacity_kwh * default_soc_pct / 100.0
                _log.warning(
                    "MilpHigsOptimizer: no SoC for %r — assuming %.0f%%",
                    sc.device_id,
                    default_soc_pct,
                )
        return result

    @staticmethod
    def _initial_threshold_values(
        threshold_devices: list[ThresholdConstraints],
        context: OptimizationContext,
    ) -> dict[str, float]:
        """Return initial measured value keyed by device_id.

        Reads ``DeviceState.extra["measured_value"]``; falls back to the
        midpoint of [bottom_threshold, top_threshold] when absent.
        """
        result: dict[str, float] = {}
        for tc in threshold_devices:
            state = context.device_states.get(tc.device_id)
            measured: float | None = None
            if state is not None:
                raw = state.extra.get("measured_value")
                if raw is not None:
                    measured = float(raw)
            if measured is None:
                measured = (tc.bottom_threshold + tc.top_threshold) / 2.0
                _log.warning(
                    "MilpHigsOptimizer: no measured_value for threshold device %r"
                    " — assuming midpoint %.2f %s",
                    tc.device_id,
                    measured,
                    tc.unit,
                )
            # Clamp into the allowed band: an out-of-bounds live value (sensor
            # overshoot past a threshold) would otherwise make the val[] bounds
            # infeasible and collapse the entire plan. The emergency override
            # in the contributor handles the real out-of-bounds correction.
            if not (tc.bottom_threshold <= measured <= tc.top_threshold):
                clamped = min(max(measured, tc.bottom_threshold), tc.top_threshold)
                _log.warning(
                    "MilpHigsOptimizer: measured value %.2f %s for %r is outside"
                    " [%.2f, %.2f] — clamping to %.2f for planning",
                    measured,
                    tc.unit,
                    tc.device_id,
                    tc.bottom_threshold,
                    tc.top_threshold,
                    clamped,
                )
                measured = clamped
            result[tc.device_id] = measured
        return result

    @staticmethod
    def _initial_threshold_running(
        threshold_devices: list[ThresholdConstraints],
        context: OptimizationContext,
    ) -> dict[str, bool]:
        """Return whether each threshold device is currently running.

        Uses the same >50%-of-rated-power heuristic as the control
        contributor. Needed so the min-runtime/offtime constraints see the
        correct on/off state at the plan boundary (step 0).
        """
        result: dict[str, bool] = {}
        for tc in threshold_devices:
            state = context.device_states.get(tc.device_id)
            result[tc.device_id] = (
                state is not None
                and state.power_w is not None
                and state.power_w > tc.rated_power_kw * 500.0
            )
        return result

    def _get_solver(self) -> pulp.LpSolver:
        """Return the HiGHS solver; fall back to CBC if unavailable.

        Both the MIP gap and the time limit follow ``precision`` (see class
        docstring). The time limit is the safety net: pulp maps a HiGHS
        time-limit stop to LpStatusOptimal with the best incumbent, so the
        plan degrades to "best found in N seconds" instead of going empty.
        The limit is only omitted at precision 1.0 (legacy exact mode).
        """
        p = self._precision
        gap = 10.0 ** -(2.0 + 7.0 * p)
        time_limit = None if p >= 1.0 else 5.0 + 55.0 * p
        if "HiGHS" in pulp.listSolvers(onlyAvailable=True):
            return pulp.HiGHS(msg=False, gapAbs=gap, gapRel=gap, timeLimit=time_limit)
        _log.warning("MilpHigsOptimizer: HiGHS not available — falling back to CBC")
        # No timeLimit for CBC: pulp maps its time-limit stop to "Not Solved",
        # which the caller would turn into an empty plan.
        return pulp.PULP_CBC_CMD(msg=False, gapRel=gap)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────────────────────────────


def _extract_threshold_intents(
    threshold_devices: list[ThresholdConstraints],
    variables: dict,
    timestamps: list[datetime],
) -> list[ControlIntent]:
    """Convert threshold device solver values into ``ControlIntent`` objects.

    Modes emitted:
    ``"run"``      — optimizer scheduled the device to run this step.
    ``"standby"``  — optimizer scheduled the device to be off this step.
    """
    run = variables.get("run", {})
    val = variables.get("val", {})
    intents: list[ControlIntent] = []
    for tc in threshold_devices:
        d = tc.device_id
        for t, ts in enumerate(timestamps):
            run_val = pulp.value(run.get((d, t))) or 0.0
            is_running = run_val > 0.5
            predicted_value = pulp.value(val.get((d, t)))
            intents.append(ControlIntent(
                device_id=d,
                timestep=ts,
                power_kw=round(tc.rated_power_kw, 4) if is_running else 0.0,
                grid_allowed=True,
                # stored_energy_kwh repurposed here to carry the predicted
                # measured value at end of step (°C, %RH, etc.) for UI display.
                stored_energy_kwh=round(predicted_value, 4) if predicted_value is not None else None,
            ))
    return intents


def _interpolate_kw(
    points: list[ForecastPoint], timestamps: list[datetime]
) -> list[float]:
    """Map forecast points onto *timestamps* via forward-fill (hold-previous).

    A point's value holds from its timestamp until the next point — the
    natural semantics of hourly forecasts and prices (the 13:00 value applies
    to the whole 13:00–14:00 hour) and the same convention the UI's
    ``/api/forecast`` series uses, so plan bars and forecast lines stay
    aligned.  (Nearest-neighbour shifted everything half a step early:
    12:45 took 13:00's value.)  Before the first point the first value is
    held.  Returns ``0.0`` for every timestamp when *points* is empty.
    """
    if not points:
        return [0.0] * len(timestamps)
    pts = sorted(points, key=lambda p: p.timestamp)
    out: list[float] = []
    j = 0
    prev = pts[0]
    for ts in timestamps:  # timestamps are ascending
        while j < len(pts) and pts[j].timestamp <= ts:
            prev = pts[j]
            j += 1
        out.append(float(prev.value))
    return out


def _extract_ev_intents(
    ev_goals: list[EvChargingGoal],
    variables: dict,
    timestamps: list[datetime],
    step_h: float,
) -> list[ControlIntent]:
    """Convert EV solver values into ``ControlIntent`` objects (phase-1 only).

    Phase-2 intents (mandatory top-off) are appended by the caller.

    ``grid_allowed`` comes directly from the solved ``ev_grid`` decision
    binary: 1 = grid-sourced slot (Instant Charging), 0 = PV slot where the
    planned energy was constrained to the forecast PV surplus (executes as
    openWB PV mode tracking the real surplus).  pv_only goals have the
    binary fixed to 0.  Unplanned steps fall through to PV mode in
    EvChargerContributor.
    """
    ev = variables.get("ev", {})
    ev_grid = variables.get("ev_grid", {})
    intents: list[ControlIntent] = []

    for goal in ev_goals:
        if not goal.connected:
            continue
        for t, ts in enumerate(timestamps):
            if ts >= goal.phase2_start_time:
                continue  # phase2 intents added separately
            # ev variables are in Wh — convert to kWh for ControlIntent
            key = (goal.asset_id, t)
            ev_wh = pulp.value(ev.get(key)) or 0.0
            ev_kwh = ev_wh / _WH
            if ev_kwh > 0.01:
                grid_allowed = (pulp.value(ev_grid.get(key)) or 0.0) > 0.5
                intents.append(ControlIntent(
                    device_id=goal.device_id,
                    timestep=ts,
                    power_kw=round(ev_wh / step_h / _WH, 4),   # Wh/h / 1000 = kW
                    grid_allowed=grid_allowed,
                    reserved_kwh=round(ev_kwh, 4),
                ))
    return intents


def _extract_intents(
    batteries: list[StorageConstraints],
    variables: dict,
    timestamps: list[datetime],
    step_h: float,
) -> list[ControlIntent]:
    """Convert solver values into ``ControlIntent`` objects."""
    c = variables["c"]
    d = variables["d"]
    e = variables["e"]
    g_exp = variables["g_exp"]
    intents: list[ControlIntent] = []

    for sc in batteries:
        b = sc.device_id
        for t, ts in enumerate(timestamps):
            # All LP energy variables are in Wh — convert to kW/kWh for ControlIntent.
            c_wh = pulp.value(c[(b, t)]) or 0.0
            d_wh = pulp.value(d[(b, t)]) or 0.0
            e_wh = pulp.value(e[(b, t)]) or 0.0
            # Wh / h = W (average power over the step)
            c_w = c_wh / step_h
            d_w = d_wh / step_h
            c_kwh = c_wh / _WH
            d_kwh = d_wh / _WH
            e_kwh = e_wh / _WH

            if c_w > 1.0:
                intents.append(
                    ControlIntent(
                        device_id=b,
                        timestep=ts,
                        power_kw=round(c_w / _WH, 4),
                        grid_allowed=not sc.no_grid_charge,
                        reserved_kwh=round(c_kwh, 4),
                        stored_energy_kwh=round(e_kwh, 4),
                    )
                )
            elif d_w > 1.0:
                g_exp_wh = pulp.value(g_exp[t]) or 0.0
                is_feed_in = g_exp_wh > 1.0   # > 1 Wh per step ≈ 4 W average
                intents.append(
                    ControlIntent(
                        device_id=b,
                        timestep=ts,
                        power_kw=round(-d_w / _WH, 4),
                        grid_allowed=False,
                        export_allowed=is_feed_in,
                        reserved_kwh=round(-d_kwh, 4),
                        stored_energy_kwh=round(e_kwh, 4),
                    )
                )
            else:
                # Idle: absorb whatever PV surplus arrives (power_kw=0, grid_allowed=False).
                intents.append(
                    ControlIntent(
                        device_id=b,
                        timestep=ts,
                        power_kw=0.0,
                        grid_allowed=False,
                        reserved_kwh=0.0,
                        stored_energy_kwh=round(e_kwh, 4),
                    )
                )

    return intents

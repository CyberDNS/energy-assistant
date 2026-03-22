"""MilpHigsOptimizer — cost-minimising MILP optimizer using the HiGHS solver.

Algorithm overview
------------------
Time is discretised into fixed-length steps of ``step_minutes`` (default 60).
For each storage device *b* (taken from ``context.storage_constraints``) and
each time step *t* the model introduces four groups of decision variables:

    c[b,t]  — AC energy charged into the battery   (kWh, ≥ 0)
    d[b,t]  — AC energy discharged from the battery (kWh, ≥ 0)
    u[b,t]  — binary: 1 = charging, 0 = discharging/idle
    e[b,t]  — stored energy at END of step *t*       (kWh)

Grid energy consumed per step (positive = import, negative = export):

    g[t] = net_load[t] + Σ_b c[b,t] − Σ_b d[b,t]

SoC dynamics (with charge efficiency ηc and discharge efficiency ηd):

    e[b,t] = e[b,t−1] + ηc · c[b,t] − d[b,t] / ηd

Grid energy split into import and export (both ≥ 0):

    g_imp[t]  — energy drawn from the grid  (kWh, ≥ 0)
    g_exp[t]  — energy fed into the grid    (kWh, ≥ 0)
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

* ``"grid_fill"``  — charge from grid/PV at up to ``max_power_w`` W.
* ``"discharge"``  — discharge to home at up to ``−min_power_w`` W.
* ``"idle"``       — no action requested.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pulp

from ...core.models import (
    ControlIntent,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
    TariffPoint,
)
from ...core.optimizer import OptimizationContext

_log = logging.getLogger(__name__)

# Default electricity price used when no tariff or forecast is available.
_DEFAULT_PRICE_EUR_KWH = 0.30


class MilpHigsOptimizer:
    """Cost-minimising MILP optimizer backed by the HiGHS solver (via PuLP).

    Parameters
    ----------
    step_minutes:
        Duration of each planning time step in minutes.  Must be a divisor
        of 60 or a multiple of 60.  Default is 60 (one-hour steps).
    """

    def __init__(self, step_minutes: int = 60) -> None:
        self._step_min = step_minutes

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
            return EnergyPlan(horizon_hours=horizon_h)

        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
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
        net_load = [(c - p) * step_h for c, p in zip(consumption_kw, pv_kw)]

        # ── Storage devices ────────────────────────────────────────────
        batteries = context.storage_constraints
        if not batteries:
            _log.info("MilpHigsOptimizer: no storage constraints — returning empty plan")
            return EnergyPlan(horizon_hours=horizon_h)

        initial_energy = self._initial_energy(batteries, context)

        # ── Build and solve the MILP model ─────────────────────────────
        prob, variables = self._build_model(
            n_steps, step_h, batteries, net_load, prices, export_prices,
            initial_energy, context.battery_cost_basis,
        )
        status = prob.solve(self._get_solver())

        if pulp.LpStatus[status] not in ("Optimal", "Feasible"):
            _log.warning(
                "MilpHigsOptimizer: solver returned %r — emitting empty plan",
                pulp.LpStatus[status],
            )
            return EnergyPlan(horizon_hours=horizon_h)

        # ── Extract schedule → EnergyPlan ─────────────────────────────
        intents = _extract_intents(batteries, variables, timestamps, step_h)
        return EnergyPlan(horizon_hours=horizon_h, intents=intents)

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

        # ── Decision variables ─────────────────────────────────────────
        c: dict[tuple[str, int], pulp.LpVariable] = {}  # charge energy (kWh)
        d: dict[tuple[str, int], pulp.LpVariable] = {}  # discharge energy (kWh)
        u: dict[tuple[str, int], pulp.LpVariable] = {}  # binary: 1 = charging
        e: dict[tuple[str, int], pulp.LpVariable] = {}  # stored energy (kWh)

        for sc in batteries:
            b = sc.device_id
            e_min = sc.capacity_kwh * sc.min_soc_pct / 100.0
            e_max = sc.capacity_kwh * sc.max_soc_pct / 100.0
            for t in T:
                c[(b, t)] = pulp.LpVariable(f"c__{b}__{t}", lowBound=0)
                d[(b, t)] = pulp.LpVariable(f"d__{b}__{t}", lowBound=0)
                u[(b, t)] = pulp.LpVariable(f"u__{b}__{t}", cat="Binary")
                e[(b, t)] = pulp.LpVariable(f"e__{b}__{t}", lowBound=e_min, upBound=e_max)

        # Grid energy per step: split into import (≥0) and export (≥0)
        g_imp = {t: pulp.LpVariable(f"g_imp__{t}", lowBound=0) for t in T}
        g_exp = {t: pulp.LpVariable(f"g_exp__{t}", lowBound=0) for t in T}

        # ── Objective ─────────────────────────────────────────────────
        # Grid cost over the horizon
        grid_cost = pulp.lpSum(
            prices[t] * g_imp[t] - export_prices[t] * g_exp[t] for t in T
        )
        # Terminal value: energy remaining at end of horizon is worth
        # tv_basis €/kWh — subtract it (it reduces net cost).
        # tv_basis is SEPARATE from battery_cost_basis (the discharge threshold):
        # it should reflect the expected *future market value* of stored energy
        # so the optimizer is incentivised to refill from PV whenever future
        # dispatch prices exceed the effective charging cost.
        tv_basis = terminal_value_basis if terminal_value_basis is not None else (battery_cost_basis or {})
        terminal_value = pulp.lpSum(
            tv_basis.get(sc.device_id, 0.0) * e[(sc.device_id, n_steps - 1)]
            for sc in batteries
        )
        # Degradation cost: each kWh stored costs purchase_price/(cycle_life*capacity)
        # Applied to η_c × c[b,t] (the kWh actually stored per AC kWh charged).
        # This prevents the optimizer from cycling cheaply-charged energy out at a
        # small margin that doesn't cover battery wear.
        degradation_cost = pulp.lpSum(
            sc.degradation_cost_per_kwh * sc.charge_efficiency * c[(sc.device_id, t)]
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
        # discharges first and fastest.  Swapping which battery fires at step t
        # vs t+1 changes this integral, so the degeneracy is broken.
        #
        # Scale ε = 1e-5:
        #   • Benefit of one step of early Zendure discharge (at step t of T):
        #       ε × (0.175/0.95) × (T − t)  ≈  1e-5 × 0.184 × 48  ≈  8.8e-5 €
        #   • Well above HiGHS gapAbs=1e-9 → tie always resolved.
        #   • Well below price economics (~1e-3 €/step) → no real decisions changed.
        sorted_by_deg = sorted(batteries, key=lambda sc: sc.degradation_cost_per_kwh)
        if len(sorted_by_deg) >= 2:
            pb = sorted_by_deg[0].device_id   # cheapest-to-wear battery
            priority_tiebreak = 1e-5 * pulp.lpSum(e[(pb, t)] for t in T)
        else:
            priority_tiebreak = 0

        prob += grid_cost + degradation_cost + priority_tiebreak - terminal_value, "total_cost"

        # ── Constraints ───────────────────────────────────────────────
        for t in T:
            # Grid energy balance: import − export = net demand
            prob += (
                g_imp[t] - g_exp[t]
                == net_load[t]
                + pulp.lpSum(c[(sc.device_id, t)] for sc in batteries)
                - pulp.lpSum(d[(sc.device_id, t)] for sc in batteries),
                f"grid_balance__{t}",
            )

        for sc in batteries:
            b = sc.device_id
            c_max_kwh = sc.max_charge_kw * step_h
            d_max_kwh = sc.max_discharge_kw * step_h
            eta_c = sc.charge_efficiency
            eta_d = sc.discharge_efficiency
            e_init = initial_energy[b]

            for t in T:
                # Charge only when u=1; discharge only when u=0
                prob += c[(b, t)] <= c_max_kwh * u[(b, t)], f"c_max__{b}__{t}"
                prob += d[(b, t)] <= d_max_kwh * (1 - u[(b, t)]), f"d_max__{b}__{t}"

                # SoC dynamics
                e_prev = e_init if t == 0 else e[(b, t - 1)]
                prob += (
                    e[(b, t)] == e_prev + eta_c * c[(b, t)] - d[(b, t)] / eta_d,
                    f"soc__{b}__{t}",
                )

        variables = {"c": c, "d": d, "u": u, "e": e, "g_imp": g_imp, "g_exp": g_exp}
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
            state = context.device_states.get(sc.device_id)
            if state is not None and state.soc_pct is not None:
                result[sc.device_id] = sc.capacity_kwh * state.soc_pct / 100.0
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
    def _get_solver() -> pulp.LpSolver:
        """Return the HiGHS solver; fall back to CBC if unavailable."""
        if "HiGHS" in pulp.listSolvers(onlyAvailable=True):
            # gapAbs=1e-9 ensures the solver resolves degenerate battery-dispatch
            # tie-breaks (preference differences ~1e-4 €) that would otherwise be
            # swallowed by the default absolute MIP gap tolerance (~5e-4 €).
            return pulp.HiGHS(msg=False, gapAbs=1e-9, gapRel=1e-9)
        _log.warning("MilpHigsOptimizer: HiGHS not available — falling back to CBC")
        return pulp.PULP_CBC_CMD(msg=False)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────────────────────────────


def _nearest(
    sorted_points: list[ForecastPoint], ts: datetime
) -> float:
    """Return the value from the nearest point (by time) to *ts*."""
    best = min(sorted_points, key=lambda p: abs((p.timestamp - ts).total_seconds()))
    return best.value


def _interpolate_kw(
    points: list[ForecastPoint], timestamps: list[datetime]
) -> list[float]:
    """Map forecast points onto *timestamps* via nearest-neighbour lookup.

    Returns ``0.0`` for every timestamp when *points* is empty.
    """
    if not points:
        return [0.0] * len(timestamps)
    sorted_pts = sorted(points, key=lambda p: p.timestamp)
    return [_nearest(sorted_pts, ts) for ts in timestamps]


def _extract_intents(
    batteries: list[StorageConstraints],
    variables: dict,
    timestamps: list[datetime],
    step_h: float,
) -> list[ControlIntent]:
    """Convert solver values into ``ControlIntent`` objects."""
    c = variables["c"]
    d = variables["d"]
    intents: list[ControlIntent] = []

    for sc in batteries:
        b = sc.device_id
        for t, ts in enumerate(timestamps):
            c_kwh = pulp.value(c[(b, t)]) or 0.0
            d_kwh = pulp.value(d[(b, t)]) or 0.0
            # Convert from kWh per step back to average W
            c_w = c_kwh / step_h * 1000.0
            d_w = d_kwh / step_h * 1000.0

            if c_w > 1.0:
                # Platform sign: positive power = charging
                intents.append(
                    ControlIntent(
                        device_id=b,
                        timestep=ts,
                        mode="grid_fill",
                        min_power_w=0.0,
                        max_power_w=round(c_w, 1),
                    )
                )
            elif d_w > 1.0:
                # Platform sign: negative power = discharging
                intents.append(
                    ControlIntent(
                        device_id=b,
                        timestep=ts,
                        mode="discharge",
                        min_power_w=round(-d_w, 1),
                        max_power_w=0.0,
                    )
                )
            else:
                intents.append(
                    ControlIntent(device_id=b, timestep=ts, mode="idle")
                )

    return intents

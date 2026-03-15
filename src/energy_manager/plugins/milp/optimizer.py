"""
MILP-based battery scheduling optimizer.

Problem
-------
Given a look-ahead horizon (typically 24 h) discretized into 1-hour slots,
find the charge/discharge schedule for all controllable storage devices that
minimises the total grid electricity cost while respecting battery physics.

Devices declare themselves controllable by providing a ``StorageConstraints``
object (populated from config) and placing it in
``OptimizationContext.storage_constraints``.  The optimizer then schedules
every declared device without knowing the concrete plugin type.

Decision variables (per battery *n*, per slot *t*)
---------------------------------------------------
``c[n][t]``   ≥ 0   Charge power (kW)
``d[n][t]``   ≥ 0   Discharge power (kW)
``b[n][t]``   ∈{0,1} 1 = charging mode (binary — the "MI" in MILP)
``soc[n][t]`` ≥ 0  State of charge at *end* of slot t (kWh)

Shared per-slot variables
-------------------------
``g_imp[t]`` ≥ 0  Grid import power (kW)
``g_exp[t]`` ≥ 0  Grid export power (kW)

Energy-balance constraint per slot
------------------------------------
  g_imp[t] - g_exp[t]  =  load_kw  -  pv_kw[t]  +  Σ_n ( c[n][t] - d[n][t] )

SoC dynamics (per battery n)
-----------------------------
  soc[n][t] = soc[n][t-1]  +  η_c·c[n][t]·Δt  -  (1/η_d)·d[n][t]·Δt

Objective
---------
  min  Σ_t  [ price[t] · g_imp[t] · Δt  −  feed_in_price · g_exp[t] · Δt ]
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pulp

from ...core.models import (
    ControlAction,
    EnergyPlan,
    ForecastQuantity,
    StorageConstraints,
)
from ...core.optimizer import OptimizationContext

log = logging.getLogger(__name__)


class MILPOptimizer:
    """
    Mixed-Integer Linear Program optimizer for battery scheduling.

    Reads all controllable storage devices from
    ``OptimizationContext.storage_constraints`` and schedules them together
    in one MILP problem, without knowing which specific plugin type they are.

    Parameters
    ----------
    tariff_id:
        Which tariff in ``OptimizationContext.tariffs`` to use for electricity
        prices.  Falls back to ``"default"`` when ``None``.
    slot_hours:
        Duration of each planning slot in hours (default 1.0).
    baseline_load_kw:
        Assumed constant household load (kW).  Used for grid-balance modelling
        when no load forecast is available.  Default 0.3 kW (300 W).
    feed_in_price_eur_per_kwh:
        Revenue for grid export in EUR/kWh.  Set to 0 if you don't sell back.
        Default 0.0.
    solver_msg:
        Whether to show the CBC solver output (default False).
    """

    def __init__(
        self,
        tariff_id: str | None = None,
        slot_hours: float = 1.0,
        baseline_load_kw: float = 0.3,
        feed_in_price_eur_per_kwh: float = 0.0,
        solver_msg: bool = False,
    ) -> None:
        self._tariff_id = tariff_id
        self._slot_hours = slot_hours
        self._baseline_load_kw = baseline_load_kw
        self._feed_in = feed_in_price_eur_per_kwh
        self._solver_msg = solver_msg

    # ------------------------------------------------------------------
    # Optimizer protocol
    # ------------------------------------------------------------------

    async def optimize(self, context: OptimizationContext) -> EnergyPlan:
        """
        Solve the MILP for all controllable storage devices in context and
        return an EnergyPlan with per-slot setpoints for each device.
        """
        batteries = context.storage_constraints
        if not batteries:
            log.warning("MILPOptimizer: no storage_constraints in context — returning empty plan")
            return EnergyPlan(horizon_hours=int(context.horizon.total_seconds() / 3600))

        dt = self._slot_hours

        # 1. Per-battery initial SoC, honoring live device-reported bounds -----
        soc_0_list: list[float] = []
        soc_min_list: list[float] = []
        soc_max_list: list[float] = []
        for sc in batteries:
            state = context.device_states.get(sc.device_id)
            soc_pct_0 = (state.soc_pct or 50.0) if state else 50.0
            if state:
                min_soc_pct = state.extra.get("min_soc_pct") or sc.min_soc_pct
                max_soc_pct = state.extra.get("max_soc_pct") or sc.max_soc_pct
            else:
                min_soc_pct, max_soc_pct = sc.min_soc_pct, sc.max_soc_pct
            soc_0_list.append(soc_pct_0 / 100.0 * sc.capacity_kwh)
            soc_min_list.append(min_soc_pct / 100.0 * sc.capacity_kwh)
            soc_max_list.append(max_soc_pct / 100.0 * sc.capacity_kwh)

        # 2. Tariff prices → slot-aligned price array --------------------------
        tariff = self._resolve_tariff(context)
        slot_starts = self._slot_timestamps(context.horizon)
        T = len(slot_starts)
        if T == 0:
            log.warning("MILPOptimizer: no planning slots — returning empty plan")
            return EnergyPlan(horizon_hours=int(context.horizon.total_seconds() / 3600))

        prices = await self._prices_per_slot(tariff, slot_starts)

        # 3. PV forecast → slot-aligned power array (kW) ----------------------
        pv_kw = self._pv_per_slot(
            context.forecasts.get(ForecastQuantity.PV_GENERATION, []),
            slot_starts,
        )

        # 4. Build and solve MILP with all batteries ---------------------------
        schedule = self._solve(
            T, dt, soc_0_list, soc_min_list, soc_max_list, batteries, prices, pv_kw
        )

        # 5. Assemble EnergyPlan — one action per (device, slot) ---------------
        actions: list[ControlAction] = [
            ControlAction(
                device_id=device_id,
                command="set_automation_limit",
                value=round(net_kw * 1000),  # kW → W (int)
                scheduled_at=slot_start,
            )
            for device_id, net_kw_list in schedule.items()
            for net_kw, slot_start in zip(net_kw_list, slot_starts)
        ]
        actions.sort(key=lambda a: (a.scheduled_at, a.device_id))

        return EnergyPlan(
            horizon_hours=int(context.horizon.total_seconds() / 3600),
            actions=actions,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _slot_timestamps(self, horizon: timedelta) -> list[datetime]:
        """Return UTC datetimes marking the start of each planning slot."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        slot_delta = timedelta(hours=self._slot_hours)
        slots: list[datetime] = []
        t = now
        while t < now + horizon:
            slots.append(t)
            t += slot_delta
        return slots

    def _resolve_tariff(self, context: OptimizationContext):  # type: ignore[return]
        """Return the tariff model to use, or None if none configured."""
        if not context.tariffs:
            return None
        if self._tariff_id and self._tariff_id in context.tariffs:
            return context.tariffs[self._tariff_id]
        if "default" in context.tariffs:
            return context.tariffs["default"]
        return next(iter(context.tariffs.values()))

    async def _prices_per_slot(
        self,
        tariff,  # TariffModel | None
        slot_starts: list[datetime],
    ) -> list[float]:
        """Return electricity price (EUR/kWh) for each slot."""
        if tariff is None:
            return [0.25] * len(slot_starts)  # fallback flat price
        prices: list[float] = []
        for ts in slot_starts:
            try:
                p = await tariff.price_at(ts)
            except Exception:
                p = 0.25
            prices.append(p)
        return prices

    def _pv_per_slot(
        self,
        forecast_points: list,  # list[ForecastPoint]
        slot_starts: list[datetime],
    ) -> list[float]:
        """Align PV forecast (W) to planning slots; return kW per slot."""
        if not forecast_points:
            return [0.0] * len(slot_starts)

        # Build a mapping from hour-truncated UTC timestamp → power_W
        pv_map: dict[datetime, float] = {}
        for pt in forecast_points:
            hour_ts = pt.timestamp.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
            # Average across multiple points within the same hour
            if hour_ts in pv_map:
                pv_map[hour_ts] = (pv_map[hour_ts] + pt.value) / 2.0
            else:
                pv_map[hour_ts] = pt.value

        return [
            pv_map.get(
                ts.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc),
                0.0,
            ) / 1000.0  # W → kW
            for ts in slot_starts
        ]

    def _solve(
        self,
        T: int,
        dt: float,
        soc_0_list: list[float],
        soc_min_list: list[float],
        soc_max_list: list[float],
        batteries: list[StorageConstraints],
        prices: list[float],
        pv_kw: list[float],
    ) -> dict[str, list[float]]:
        """
        Build and solve the MILP for N batteries.
        Returns ``{device_id: [net_kw_per_slot]}`` where net_kw > 0 = discharge.
        """
        N = len(batteries)
        prob = pulp.LpProblem("battery_schedule", pulp.LpMinimize)

        # --- Per-battery decision variables ---------------------------------
        c   = [[pulp.LpVariable(f"c_{n}_{t}",   lowBound=0, upBound=batteries[n].max_charge_kw)    for t in range(T)] for n in range(N)]
        d   = [[pulp.LpVariable(f"d_{n}_{t}",   lowBound=0, upBound=batteries[n].max_discharge_kw) for t in range(T)] for n in range(N)]
        b   = [[pulp.LpVariable(f"b_{n}_{t}",   cat="Binary")                                       for t in range(T)] for n in range(N)]
        soc = [[pulp.LpVariable(f"soc_{n}_{t}", lowBound=soc_min_list[n], upBound=soc_max_list[n]) for t in range(T)] for n in range(N)]

        # --- Shared grid variables ------------------------------------------
        g_imp = [pulp.LpVariable(f"gimp_{t}", lowBound=0) for t in range(T)]
        g_exp = [pulp.LpVariable(f"gexp_{t}", lowBound=0) for t in range(T)]

        # --- Constraints ----------------------------------------------------
        for t in range(T):
            # Grid balance: import - export = load - pv + Σ_n (charge_n - discharge_n)
            net_battery = pulp.lpSum(c[n][t] - d[n][t] for n in range(N))
            prob += (
                g_imp[t] - g_exp[t] == self._baseline_load_kw - pv_kw[t] + net_battery,
                f"grid_balance_{t}",
            )

        for n in range(N):
            sc = batteries[n]
            for t in range(T):
                # Charge/discharge mutex via binary variable
                prob += c[n][t] <= sc.max_charge_kw    * b[n][t],       f"charge_mode_{n}_{t}"
                prob += d[n][t] <= sc.max_discharge_kw * (1 - b[n][t]), f"discharge_mode_{n}_{t}"
                # SoC dynamics
                prev_soc = soc_0_list[n] if t == 0 else soc[n][t - 1]
                prob += (
                    soc[n][t] == prev_soc
                        + sc.charge_efficiency * c[n][t] * dt
                        - (1 / sc.discharge_efficiency) * d[n][t] * dt,
                    f"soc_dynamics_{n}_{t}",
                )

        # --- Objective: minimise net grid cost ------------------------------
        prob += pulp.lpSum(
            prices[t] * g_imp[t] * dt - self._feed_in * g_exp[t] * dt
            for t in range(T)
        )

        # --- Solve ----------------------------------------------------------
        solver = pulp.PULP_CBC_CMD(msg=self._solver_msg)
        status = prob.solve(solver)

        if pulp.LpStatus[status] not in ("Optimal", "Not Solved"):
            log.warning("MILP returned status '%s'; returning zero schedule", pulp.LpStatus[status])
            return {sc.device_id: [0.0] * T for sc in batteries}

        # Extract net power per battery per slot (positive = discharge)
        return {
            batteries[n].device_id: [
                pulp.value(d[n][t]) - pulp.value(c[n][t])  # type: ignore[operator]
                for t in range(T)
            ]
            for n in range(N)
        }

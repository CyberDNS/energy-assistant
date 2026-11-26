"""Why the plan does what it does: shadow prices and reasons per slot.

After the MILP is solved, its binaries (charge/discharge direction, EV and
threshold on/off, …) are fixed to their solved values and the remaining LP
is solved once more.  An LP has dual values (shadow prices), a MILP has
not; with the binaries fixed they describe the solved plan:

``grid_balance__t``   dual μ_t: what one more kWh of demand in slot *t*
                      costs (€/kWh) — the import price while the site
                      imports, the feed-in price while it exports, more
                      than the import price when a grid limit or power
                      charge binds.
``soc__b__t``         dual −V_t: what one more kWh stored in battery *b* at
                      the end of slot *t* is worth (€/kWh).

Battery economics follow directly: charging one AC kWh costs μ_t (plus wear)
and stores η_c kWh worth V_t; discharging one AC kWh saves μ_t and uses
1/η_d kWh worth V_t.  So the plan charges when η_c·(V_t − wear) ≥ μ_t,
discharges when μ_t ≥ V_t/η_d and otherwise holds.  The reasons below are
templates over these numbers, the solved flows and the constraints that
bind ("limited by").  The primal solution of the MILP is left untouched:
the re-solve only reads duals, then every variable gets its MILP value back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pulp

from ...assets.ev import EvChargingGoal
from ...core.models import StorageConstraints, ThresholdConstraints

_log = logging.getLogger(__name__)

_WH = 1000.0
# Energy below this (Wh per slot) counts as "nothing".
_EPS_WH = 1.0
# A dual below this (€/Wh) is zero: the constraint does not bind.
_EPS_DUAL = 1e-9
# Time limit of the LP re-solve (s).  It is an LP of the size of the MILP's
# relaxation, solved in well under a second on a full 48 h horizon.
_LP_TIME_LIMIT_S = 10.0

#: Readable names of the binding-constraint codes.
CONSTRAINT_LABELS: dict[str, str] = {
    "min_soc": "minimum state of charge",
    "max_soc": "maximum state of charge",
    "max_charge_power": "maximum charge power",
    "max_discharge_power": "maximum discharge power",
    "no_grid_charge": "charging from PV only",
    "house_load": "house load (feed-in pays too little)",
    "grid_limit": "grid connection limit",
    "power_charge": "power charge",
    "ev_deadline": "charge deadline",
    "ev_mandatory": "instant charging before the deadline",
    "pv_surplus": "PV surplus",
    "threshold_top": "upper threshold",
    "threshold_bottom": "lower threshold",
}


@dataclass
class SlotReason:
    """Explanation of one device's plan in one slot."""

    reason: str
    binding: list[str] = field(default_factory=list)
    energy_value: float | None = None
    """Battery only: €/kWh of one more kWh stored at the end of the slot."""


@dataclass
class PlanExplanation:
    """Everything ``explain`` found; empty when the duals are unavailable."""

    marginal: list[float | None] = field(default_factory=list)
    """Per slot: μ_t, cost of one more kWh of demand (€/kWh)."""
    reasons: dict[tuple[str, int], SlotReason] = field(default_factory=dict)
    """Per (device_id, slot)."""


# ──────────────────────────────────────────────────────────────────────────
# Dual values
# ──────────────────────────────────────────────────────────────────────────


def solve_duals(prob: pulp.LpProblem) -> dict[str, float] | None:
    """Duals of *prob* (already solved as a MILP) with its integers fixed.

    Returns ``{constraint name: dual}`` (objective units per unit of
    right-hand side), or None when HiGHS is missing or the LP fails.  Every
    variable keeps its MILP value; categories and bounds are restored.
    """
    if "HiGHS" not in pulp.listSolvers(onlyAvailable=True):
        return None
    variables = prob.variables()
    values = {v.name: v.varValue for v in variables}
    fixed = [(v, v.cat, v.lowBound, v.upBound) for v in variables if v.cat == pulp.LpInteger]
    try:
        for v, _, _, _ in fixed:
            val = float(round(v.varValue or 0.0))
            v.cat = pulp.LpContinuous
            v.lowBound = val
            v.upBound = val
        status = prob.solve(pulp.HiGHS(msg=False, timeLimit=_LP_TIME_LIMIT_S))
        if pulp.LpStatus[status] != "Optimal":
            _log.debug("explain: LP re-solve returned %s", pulp.LpStatus[status])
            return None
        return {name: (c.pi or 0.0) for name, c in prob.constraints.items()}
    except Exception:  # noqa: BLE001 — explanations are optional, never fail a plan
        _log.warning("explain: LP re-solve for the plan reasons failed", exc_info=True)
        return None
    finally:
        for v, cat, lo, hi in fixed:
            v.cat, v.lowBound, v.upBound = cat, lo, hi
        for v in variables:
            v.varValue = values[v.name]


# ──────────────────────────────────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────────────────────────────────


def _eur(v: float) -> str:
    return f"-€{-v:.2f}" if v < -0.005 else f"€{abs(v):.2f}"


def _kw(v: float) -> str:
    return f"{v:.1f} kW" if v >= 1 else f"{v * 1000:.0f} W"


def _when(ts: datetime, ref: datetime) -> str:
    """Local "HH:MM", with "tomorrow" or the weekday when not on *ref*'s day."""
    local, ref_local = ts.astimezone(), ref.astimezone()
    days = (local.date() - ref_local.date()).days
    if days == 0:
        return local.strftime("%H:%M")
    if days == 1:
        return local.strftime("tomorrow %H:%M")
    return local.strftime("%a %H:%M")


def _limited(binding: list[str], details: dict[str, str]) -> str:
    """" Limited by: …." for the binding constraints worth naming."""
    parts = [details.get(b, CONSTRAINT_LABELS.get(b, b)) for b in binding]
    return f" Limited by: {', '.join(parts)}." if parts else ""


# ──────────────────────────────────────────────────────────────────────────
# Reasons
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class ExplainInputs:
    """The solved model and the inputs it was built from (see optimizer)."""

    variables: dict
    batteries: list[StorageConstraints]
    ev_goals: list[EvChargingGoal]
    threshold_devices: list[ThresholdConstraints]
    timestamps: list[datetime]
    slot_starts: list[datetime]
    dur_h: list[float]
    prices: list[float]
    export_prices: list[float]
    terminal_value: dict[str, float]
    mandatory_overlap_h: dict[tuple[str, int], float]
    """Per (ev device_id, slot): hours of the forced phase-2 window."""


def explain(duals: dict[str, float], inp: ExplainInputs) -> PlanExplanation:
    """Reasons for every battery slot, EV charging slot and threshold slot."""
    out = PlanExplanation()
    n = len(inp.timestamps)
    v = inp.variables

    def val(var) -> float:
        return (pulp.value(var) or 0.0) if var is not None else 0.0

    def dual(name: str) -> float | None:
        d = duals.get(name)
        return None if d is None else d * _WH   # €/Wh → €/kWh

    marginal = [dual(f"grid_balance__{t}") for t in range(n)]
    out.marginal = [None if m is None else round(m, 4) for m in marginal]

    imports = [val(v["g_imp"][t]) + sum(val(s) for (_, tt), s in v["s_imp"].items() if tt == t)
               for t in range(n)]
    exports = [val(v["g_exp"][t]) for t in range(n)]

    def site_binding(t: int) -> list[str]:
        """Grid-side constraints binding in slot *t*."""
        out: list[str] = []
        if abs(duals.get(f"grid_limit__{t}", 0.0)) > _EPS_DUAL:
            out.append("grid_limit")
        elif (
            marginal[t] is not None and imports[t] > _EPS_WH
            and marginal[t] > inp.prices[t] + 0.005
        ):
            out.append("power_charge")
        return out

    for sc in inp.batteries:
        _explain_battery(sc, inp, duals, marginal, imports, exports, site_binding, out)
    for goal in inp.ev_goals:
        if goal.connected:
            _explain_ev(goal, inp, duals, marginal, site_binding, out)
    for tc in inp.threshold_devices:
        _explain_threshold(tc, inp, marginal, out)
    return out


def _explain_battery(sc, inp: ExplainInputs, duals, marginal, imports, exports, site_binding,
                     out: PlanExplanation) -> None:
    b = sc.device_id
    v = inp.variables
    n = len(inp.timestamps)
    eta_c, eta_d = sc.charge_efficiency, sc.discharge_efficiency
    wear = sc.degradation_cost_per_kwh
    e_min = sc.capacity_kwh * sc.min_soc_pct / 100.0 * _WH
    e_max = sc.capacity_kwh * sc.max_soc_pct / 100.0 * _WH

    c = [pulp.value(v["c"][(b, t)]) or 0.0 for t in range(n)]
    d = [pulp.value(v["d"][(b, t)]) or 0.0 for t in range(n)]
    e = [pulp.value(v["e"][(b, t)]) or 0.0 for t in range(n)]
    value = [duals.get(f"soc__{b}__{t}") for t in range(n)]
    value = [None if x is None else -x * _WH for x in value]
    if any(x is None for x in value):
        return

    def use_value(s: int) -> float:
        """€/kWh one discharged AC kWh is worth in slot *s*."""
        return inp.export_prices[s] if exports[s] > _EPS_WH else inp.prices[s]

    def next_use(t: int) -> tuple[str, float]:
        """Where energy stored in slot *t* goes: ("from 18:00 (€0.41/kWh)",
        €/kWh per AC kWh discharged) — the next discharge block, its prices
        weighted by the energy discharged — or the end of the plan."""
        s = next((s for s in range(t + 1, n) if d[s] > _EPS_WH), None)
        if s is None:
            tv = inp.terminal_value.get(b, 0.0)
            return f"beyond the end of the plan (valued {_eur(tv)}/kWh stored)", tv * eta_d
        block = [s]
        while block[-1] + 1 < n and d[block[-1] + 1] > _EPS_WH:
            block.append(block[-1] + 1)
        price = sum(use_value(k) * d[k] for k in block) / sum(d[k] for k in block)
        return f"from {_when(inp.slot_starts[s], inp.slot_starts[t])} ({_eur(price)}/kWh)", price

    def next_charge(t: int) -> str:
        s = next((s for s in range(t + 1, n) if c[s] > _EPS_WH), None)
        return "" if s is None else f", next charging at {_when(inp.slot_starts[s], inp.slot_starts[t])}"

    for t in range(n):
        dur = inp.dur_h[t]
        mu = marginal[t]
        val_t = value[t]
        if mu is None:
            continue
        c_max = sc.max_charge_kw * _WH * dur
        d_max = sc.max_discharge_kw * _WH * dur
        e_prev = e[t - 1] if t > 0 else None
        at_min = e[t] <= e_min + _EPS_WH and (e_prev is None or e_prev <= e_min + _EPS_WH)
        at_max = e[t] >= e_max - _EPS_WH and (e_prev is None or e_prev >= e_max - _EPS_WH)

        binding: list[str] = []
        details: dict[str, str] = {}
        if c[t] > _EPS_WH:
            if c[t] >= c_max - _EPS_WH:
                binding.append("max_charge_power")
                details["max_charge_power"] = f"max charge {_kw(sc.max_charge_kw)}"
            if e[t] >= e_max - _EPS_WH:
                binding.append("max_soc")
                details["max_soc"] = f"max SoC {sc.max_soc_pct:.0f}%"
            if abs(duals.get(f"no_grid_charge__{b}__{t}", 0.0)) > _EPS_DUAL:
                binding.append("no_grid_charge")
                details["no_grid_charge"] = "PV surplus only (no grid charging)"
            binding += site_binding(t)
            where, later = next_use(t)
            if not sc.no_grid_charge and imports[t] > _EPS_WH:
                # One AC kWh now: costs μ plus wear on η_c kWh stored, and
                # returns η_c·η_d kWh when used.
                gain = eta_c * eta_d * later - mu - eta_c * wear
                text = f"Charging from grid at {_eur(mu)}/kWh for use {where}"
                text += f": gain {_eur(gain)}/kWh after losses and wear." if gain > 0.005 else "."
            elif exports[t] > _EPS_WH or mu <= inp.export_prices[t] + 0.005:
                text = (
                    f"Storing PV surplus instead of feeding it in at "
                    f"{_eur(inp.export_prices[t])}/kWh, for use {where}."
                )
            else:
                text = f"Storing PV surplus for use {where}."
        elif d[t] > _EPS_WH:
            if d[t] >= d_max - _EPS_WH:
                binding.append("max_discharge_power")
                details["max_discharge_power"] = f"max discharge {_kw(sc.max_discharge_kw)}"
            if e[t] <= e_min + _EPS_WH:
                binding.append("min_soc")
                details["min_soc"] = f"min SoC {sc.min_soc_pct:.0f}%"
            worth = (
                f"the stored energy is worth {_eur(val_t / eta_d)}/kWh here "
                f"({_eur(val_t)}/kWh / {eta_d:.2f} efficiency)"
            )
            if exports[t] > _EPS_WH:
                text = f"Feeding in at {_eur(inp.export_prices[t])}/kWh; {worth}."
            else:
                if (
                    "max_discharge_power" not in binding and "min_soc" not in binding
                    and imports[t] <= _EPS_WH
                ):
                    binding.append("house_load")
                    x = inp.export_prices[t]
                    details["house_load"] = (
                        f"house load (feed-in pays only {_eur(x)}/kWh)" if x > 0.005
                        else "house load (feed-in pays nothing)"
                    )
                text = (
                    f"Discharging instead of buying grid energy at {_eur(inp.prices[t])}/kWh; "
                    f"{worth}."
                )
            binding += site_binding(t)
        elif at_min:
            binding.append("min_soc")
            text = f"Idle: battery at minimum state of charge ({sc.min_soc_pct:.0f}%){next_charge(t)}."
        elif at_max:
            where, _ = next_use(t)
            binding.append("max_soc")
            text = f"Idle: battery full ({sc.max_soc_pct:.0f}%), stored energy used {where}."
        else:
            where, _ = next_use(t)
            # One more kWh discharged now is worth μ: the import price while
            # the site imports, less once other sources cover the load.
            covered = imports[t] <= _EPS_WH and mu < inp.prices[t] - 0.005
            text = (
                f"Holding the charge for use {where}: discharging now would save only "
                f"{_eur(mu)}/kWh" + (" (the house load is already covered)." if covered else ".")
            )
        out.reasons[(b, t)] = SlotReason(
            reason=text + _limited(binding, details) if binding and not text.startswith("Idle")
            else text,
            binding=binding,
            energy_value=round(val_t, 4),
        )


def _explain_ev(goal: EvChargingGoal, inp: ExplainInputs, duals, marginal, site_binding,
                out: PlanExplanation) -> None:
    v = inp.variables
    a = goal.asset_id
    n = len(inp.timestamps)
    deadline = goal.phase2_start_time
    hard = f"ev_phase1_deadline__{a}" in duals
    deadline_binds = abs(duals.get(f"ev_phase1_deadline__{a}", 0.0)) > _EPS_DUAL
    for t in range(n):
        ref = inp.slot_starts[t]
        overlap = inp.mandatory_overlap_h.get((goal.device_id, t), 0.0)
        ev_var = v["ev"].get((a, t))
        ev_wh = (pulp.value(ev_var) or 0.0) if ev_var is not None else 0.0
        mu = marginal[t]
        if overlap > 0:
            out.reasons[(goal.device_id, t)] = SlotReason(
                reason=(
                    f"Instant charging from {_when(goal.phase2_start_time, ref)} to reach "
                    f"{goal.target_soc_pct:.0f}% by {_when(goal.target_by, ref)} "
                    f"(the last {goal.phase2_required_kwh:.1f} kWh above the "
                    f"{goal.charge_limit_soc_pct:.0f}% charge limit)."
                ),
                binding=["ev_mandatory"],
            )
            continue
        if ev_wh / _WH <= 0.01 or mu is None:
            continue
        binding: list[str] = []
        if ev_wh >= goal.max_charge_kw * _WH * inp.dur_h[t] - _EPS_WH:
            binding.append("max_charge_power")
        grid = (pulp.value(v["ev_grid"].get((a, t))) or 0.0) > 0.5
        target = (
            f" towards {goal.charge_limit_soc_pct:.0f}% by {_when(deadline, ref)}"
            if goal.phase1_required_kwh > 0.01 else ""
        )
        if not grid:
            if abs(duals.get(f"ev_pv_surplus__{a}__{t}", 0.0)) > _EPS_DUAL:
                binding.append("pv_surplus")
            text = (
                f"Charging from PV surplus{target}: feed-in would pay only "
                f"{_eur(inp.export_prices[t])}/kWh."
            )
        elif hard:
            if deadline_binds:
                binding.append("ev_deadline")
            text = (
                f"Charging from grid at {_eur(mu)}/kWh: among the cheapest slots to charge "
                f"{goal.phase1_required_kwh:.1f} kWh{target}."
            )
        else:
            text = (
                f"Charging from grid at {_eur(mu)}/kWh{target}: cheaper now than "
                "expected later in the plan."
            )
        binding += site_binding(t)
        out.reasons[(goal.device_id, t)] = SlotReason(reason=text, binding=binding)


def _explain_threshold(tc: ThresholdConstraints, inp: ExplainInputs, marginal,
                       out: PlanExplanation) -> None:
    v = inp.variables
    n = len(inp.timestamps)
    unit = tc.unit or ""
    span = f"{tc.bottom_threshold:g}–{tc.top_threshold:g}{unit}"
    for t in range(n):
        running = (pulp.value(v["run"].get((tc.device_id, t))) or 0.0) > 0.5
        value = pulp.value(v["val"].get((tc.device_id, t)))
        if value is None or marginal[t] is None:
            continue
        binding: list[str] = []
        if value >= tc.top_threshold - 1e-6:
            binding.append("threshold_top")
        elif value <= tc.bottom_threshold + 1e-6:
            binding.append("threshold_bottom")
        if running:
            text = (
                f"Running at {_eur(marginal[t])}/kWh to keep it within {span} "
                f"(predicted {value:.1f}{unit})."
            )
        else:
            text = f"Off: predicted {value:.1f}{unit} stays within {span}."
        out.reasons[(tc.device_id, t)] = SlotReason(reason=text, binding=binding)


"""Realtime one-step MILP for storage control setpoints.

This optimizer refines the active plan at the current control tick while using
only data that already exists at runtime:

- storage device limits from ``StorageConstraints``
- current live power situation
- current plan slice (active ``ControlIntent`` per device)

No extra YAML configuration is required.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulp


@dataclass(frozen=True)
class StorageSliceInput:
    """Inputs for one storage device at the current control slice."""

    device_id: str
    max_charge_w: float
    max_discharge_w: float
    no_grid_charge: bool
    export_allowed: bool
    planned_w: float
    reserved_kwh: float
    prev_setpoint_w: float = 0.0


def _solver() -> pulp.LpSolver:
    if "HiGHS" in pulp.listSolvers(onlyAvailable=True):
        return pulp.HiGHS(msg=False, gapAbs=1e-8, gapRel=1e-8)
    return pulp.PULP_CBC_CMD(msg=False)


def optimize_storage_slice(
    inputs: list[StorageSliceInput],
    *,
    grid_power_w: float,
    pv_surplus_w: float,
    dt_hours: float,
    import_price_eur_per_kwh: float,
    export_price_eur_per_kwh: float,
    cost_basis_eur_per_kwh: dict[str, float],
) -> dict[str, float] | None:
    """Return optimized per-device setpoints in W, or ``None`` on failure.

    Positive setpoint means charging, negative means discharging.
    """
    if not inputs:
        return {}

    # Keep objective in power-space (kW-equivalent) so decisions remain stable
    # even when callers pass dt_hours ~= 0 (e.g. status/diagnostics previews).
    dt_h = max(dt_hours, 1e-6)
    imp_p = max(0.0, import_price_eur_per_kwh)
    exp_p = max(0.0, export_price_eur_per_kwh)
    surplus_w = max(0.0, pv_surplus_w)

    prob = pulp.LpProblem("realtime_storage_slice", pulp.LpMinimize)

    c: dict[str, pulp.LpVariable] = {}
    d: dict[str, pulp.LpVariable] = {}
    u: dict[str, pulp.LpVariable] = {}
    dev_pos: dict[str, pulp.LpVariable] = {}
    dev_neg: dict[str, pulp.LpVariable] = {}
    slew_pos: dict[str, pulp.LpVariable] = {}
    slew_neg: dict[str, pulp.LpVariable] = {}
    idle_absorb_ids: list[str] = []

    for inp in inputs:
        did = inp.device_id
        c[did] = pulp.LpVariable(f"c__{did}", lowBound=0.0, upBound=max(0.0, inp.max_charge_w))
        d[did] = pulp.LpVariable(f"d__{did}", lowBound=0.0, upBound=max(0.0, inp.max_discharge_w))
        u[did] = pulp.LpVariable(f"u__{did}", cat="Binary")
        dev_pos[did] = pulp.LpVariable(f"dev_pos__{did}", lowBound=0.0)
        dev_neg[did] = pulp.LpVariable(f"dev_neg__{did}", lowBound=0.0)
        slew_pos[did] = pulp.LpVariable(f"slew_pos__{did}", lowBound=0.0)
        slew_neg[did] = pulp.LpVariable(f"slew_neg__{did}", lowBound=0.0)

        prob += c[did] <= inp.max_charge_w * u[did], f"charge_mode__{did}"
        prob += d[did] <= inp.max_discharge_w * (1 - u[did]), f"discharge_mode__{did}"

        # Respect strict PV-only charging requests from the current plan slice.
        if inp.no_grid_charge:
            prob += c[did] <= surplus_w, f"pv_only__{did}"

        # Idle (no planned charge or discharge) → absorb PV surplus opportunistically.
        if abs(inp.planned_w) <= 1.0:
            idle_absorb_ids.append(did)

        # Track current plan power as a soft target.
        # Platform sign convention: charge positive, discharge negative.
        prob += (
            c[did] - d[did] - inp.planned_w == dev_pos[did] - dev_neg[did],
            f"plan_deviation__{did}",
        )

        # Penalize abrupt changes to avoid 0W↔XW chatter near grid zero-crossings.
        prob += (
            c[did] - d[did] - inp.prev_setpoint_w == slew_pos[did] - slew_neg[did],
            f"setpoint_slew__{did}",
        )

        # Make planned grid charging sticky so short control ticks do not keep deferring it.
        if not inp.no_grid_charge and inp.planned_w > 1.0:
            min_follow_w = min(inp.max_charge_w, 0.6 * inp.planned_w)
            prob += c[did] >= min_follow_w, f"grid_fill_anchor__{did}"

        # If the long-term plan says discharge but we currently export PV,
        # absorb that surplus instead of forcing a rigid discharge lock.
        # For explicit export_allowed mode we keep the export intent.
        if inp.planned_w < -1.0 and surplus_w > 1.0 and not inp.export_allowed:
            pv_absorb_w = min(inp.max_charge_w, surplus_w)
            prob += c[did] >= pv_absorb_w, f"discharge_pv_absorb__{did}"

        # In grid-charging mode with PV surplus, absorb that surplus first
        # (up to planned_w) before drawing from the grid.
        # Without this, the optimizer prefers exporting PV (earns exp_p revenue)
        # while only meeting the 60 % anchor floor, leaving PV on the table.
        if not inp.no_grid_charge and surplus_w > 1.0 and inp.planned_w > 1.0:
            pv_absorb_w = min(inp.max_charge_w, surplus_w)
            prob += c[did] >= pv_absorb_w, f"charge_grid_pv_absorb__{did}"

    # Preserve idle behavior: absorb currently-exported PV surplus.
    # This keeps idle/auto intuitive at runtime (exporting while battery idles
    # would otherwise look like a broken mode decision).
    if idle_absorb_ids and surplus_w > 1.0:
        total_idle_cap_w = sum((c[did].upBound or 0.0) for did in idle_absorb_ids)
        if total_idle_cap_w > 0.0:
            absorb_target_w = min(surplus_w, total_idle_cap_w)
            prob += (
                pulp.lpSum(c[did] for did in idle_absorb_ids) >= absorb_target_w,
                "idle_absorb_surplus",
            )

    g_imp = pulp.LpVariable("g_imp", lowBound=0.0)
    g_exp = pulp.LpVariable("g_exp", lowBound=0.0)

    prob += (
        g_imp - g_exp
        == grid_power_w + pulp.lpSum(c[did] for did in c) - pulp.lpSum(d[did] for did in d),
        "grid_balance",
    )

    # Default no-export-from-battery safety: batteries may offset import,
    # but should not force additional export unless explicitly profitable.
    allow_export = False
    for inp in inputs:
        if inp.export_allowed:
            allow_export = True
            break
        basis = cost_basis_eur_per_kwh.get(inp.device_id)
        if basis is not None and basis <= exp_p:
            allow_export = True
            break

    if not allow_export:
        # Keep a small positive import buffer so the controller does not chase
        # exactly 0 W import and oscillate.
        all_no_export = all(not inp.export_allowed for inp in inputs)
        near_zero_import = abs(grid_power_w) <= 200.0
        import_buffer_w = 120.0 if (all_no_export and near_zero_import) else 0.0
        prob += (
            pulp.lpSum(d[did] for did in d)
            <= max(0.0, grid_power_w + import_buffer_w) + pulp.lpSum(c[did] for did in c),
            "no_export_from_battery",
        )

    # In discharge mode without export rights, strongly discourage under-delivery
    # relative to currently controllable import demand.
    meet_load_discharge_ids = [
        inp.device_id
        for inp in inputs
        if inp.planned_w < -1.0 and not inp.export_allowed
    ]
    unmet_import_w = None
    if meet_load_discharge_ids:
        total_cap_w = sum(d[inp_id].upBound or 0.0 for inp_id in meet_load_discharge_ids)
        target_discharge_w = min(max(0.0, grid_power_w), total_cap_w)
        unmet_import_w = pulp.LpVariable("unmet_import_w", lowBound=0.0)
        prob += (
            pulp.lpSum(d[inp_id] for inp_id in meet_load_discharge_ids) + unmet_import_w
            >= target_discharge_w,
            "meet_load_discharge_target",
        )

    grid_cost = (imp_p * g_imp - exp_p * g_exp) / 1000.0

    # Keep near the planner schedule while still allowing local adaptation.
    track_cost = 0.0
    mode_cost = 0.0
    slew_cost = 0.0
    for inp in inputs:
        did = inp.device_id
        is_discharge = inp.planned_w < -1.0
        if is_discharge:
            track_cost += 0.05 * (dev_pos[did] + dev_neg[did]) / 1000.0
            slew_cost += 0.015 * (slew_pos[did] + slew_neg[did]) / 1000.0
        else:
            track_cost += 0.2 * (dev_pos[did] + dev_neg[did]) / 1000.0
            slew_cost += 0.08 * (slew_pos[did] + slew_neg[did]) / 1000.0

        if is_discharge:
            # In discharge/feed_in mode, discourage grid charging. If PV surplus
            # exists, relax this and add a small PV-capture credit.
            penalty = 0.25 if surplus_w < 1.0 else 0.0
            mode_cost += penalty * c[did] / 1000.0
            if surplus_w > 1.0:
                mode_cost += -0.05 * c[did] / 1000.0
        elif inp.planned_w > 1.0 and not inp.no_grid_charge:
            # In grid-charging mode, discourage discharging against plan intent.
            mode_cost += 0.2 * d[did] / 1000.0

    unmet_cost = 0.0 if unmet_import_w is None else 0.6 * unmet_import_w / 1000.0
    prob += grid_cost + track_cost + mode_cost + slew_cost + unmet_cost, "objective"

    status = prob.solve(_solver())
    if pulp.LpStatus[status] not in ("Optimal", "Feasible"):
        return None

    result: dict[str, float] = {}
    for inp in inputs:
        did = inp.device_id
        c_w = float(pulp.value(c[did]) or 0.0)
        d_w = float(pulp.value(d[did]) or 0.0)
        result[did] = round(c_w - d_w, 1)
    return result

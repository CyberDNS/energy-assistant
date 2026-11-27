"""Realtime one-step MILP for storage control setpoints.

This optimizer refines the active plan at the current control tick while using
only data that already exists at runtime:

- storage device limits from ``StorageConstraints``
- current live power situation
- current plan slice (active ``ControlIntent`` per device)

No extra YAML configuration is required.

Plan fidelity
-------------
The realtime layer adapts the plan to live conditions, but it must not
overrule the plan's decisions.  The planner already priced the current slot
at the current import price, so the realtime layer does not trade the plan
against that price again; it only deviates for physical reasons.

- ``idle`` (planned 0 W) means **hold**: the plan keeps this energy for a
  later, more valuable slot.  The battery may only absorb PV surplus; it
  never discharges, whatever the current import price.  The same holds for
  charge intents (e.g. PV-only charging while there is no surplus).  (Earlier versions
  let any import price above the 0.2 €/kWh tracking penalty turn ``idle``
  into plain self-consumption, which emptied batteries before the peak.)
- A grid-charge intent delivers its planned power (within the device's
  current limits), whatever the live import price.  (Earlier versions priced
  every charged watt at the import price against a 0.2 €/kWh tracking
  penalty, so above 0.2 €/kWh planned grid charging fell to a 60 % floor.)
- A discharge intent may follow the live load, but only up to
  ``DISCHARGE_FOLLOW_HEADROOM`` × the planned power, so a small planned
  discharge does not become a full load-following discharge.
- Beyond that the battery covers load the forecast did not expect only when
  that is worth more than keeping the energy *by the plan's own value*: the
  plan states what one more kWh stored is worth (the intent's
  ``energy_value_eur_per_kwh``), so a delivered kWh is worth that ÷ the
  discharge efficiency.  When the live import price is above it (by
  ``FOLLOW_VALUE_MARGIN_EUR_PER_KWH``), the battery may discharge up to its
  limit, each extra kWh priced at its value, so the battery whose energy is
  worth least covers the extra first.  The next plan starts from the lower
  SoC and re-balances the rest of the horizon.

PV surplus is one shared pool
-----------------------------
Each battery's charge is split into a PV part and a grid part; the PV parts
of all batteries together cannot exceed the live surplus.  Only a charge
intent that allows grid charging, on a device that can charge from the grid,
has a grid part — idle and discharge intents, PV-only intents and
``no_grid_charge`` devices charge from surplus only.

When surplus is scarce it goes to the plans that can only be met from PV:
a grid-capable battery loses at most (import − export price) by taking grid
energy instead, a PV-only battery loses the energy entirely.  Surplus that is
left after every plan is served is absorbed by the batteries rather than
exported (except by export-allowed discharge intents).
"""

from __future__ import annotations

from dataclasses import dataclass

import pulp

DISCHARGE_FOLLOW_HEADROOM = 1.25
"""A discharge intent may deliver up to this multiple of its planned power
when the live load is higher than forecast (export-allowed intents excepted)."""

FOLLOW_VALUE_MARGIN_EUR_PER_KWH = 0.02
"""How far the import price must be above the value of the stored energy
before a discharge intent covers load beyond its follow headroom."""

DISCHARGE_TRACK_EUR_PER_KWH = 0.05
"""Tie-breaker keeping a discharge intent near its planned power."""

PLAN_SHORTFALL_EUR_PER_KWH = 10.0
"""Cost of a charge intent falling short of its planned power.

A lexicographic priority, not a price: it is far above any energy price, so
delivering the plan always wins over saving import cost or earning export
revenue in the current slot.  It only binds where the plan cannot be fully
met (a PV-only intent with too little surplus) and decides who gets the PV.
"""


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
    energy_value_eur_per_kwh: float | None = None
    """What one more kWh stored is worth to the plan (the intent's value);
    ``None``: unknown, the discharge stays within the follow headroom."""
    discharge_efficiency: float = 1.0


def _delivered_value(inp: StorageSliceInput) -> float | None:
    """€ per kWh delivered that the plan gives up by discharging now."""
    if inp.energy_value_eur_per_kwh is None:
        return None
    eta = inp.discharge_efficiency if inp.discharge_efficiency > 0 else 1.0
    return max(0.0, inp.energy_value_eur_per_kwh) / eta


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
    ``grid_power_w`` is the grid power without the storage devices' own
    contribution; ``pv_surplus_w`` is the surplus the storage devices may use.
    """
    if not inputs:
        return {}

    # The objective is in power-space (kW-equivalent), so decisions stay stable
    # even when callers pass dt_hours ~= 0 (e.g. status/diagnostics previews);
    # dt_hours is accepted for interface stability but not needed.
    del dt_hours
    imp_p = max(0.0, import_price_eur_per_kwh)
    exp_p = max(0.0, export_price_eur_per_kwh)
    surplus_w = max(0.0, pv_surplus_w)

    prob = pulp.LpProblem("realtime_storage_slice", pulp.LpMinimize)

    c: dict[str, pulp.LpAffineExpression] = {}
    c_pv: dict[str, pulp.LpVariable] = {}
    c_grid: dict[str, pulp.LpVariable] = {}
    d: dict[str, pulp.LpVariable] = {}
    u: dict[str, pulp.LpVariable] = {}
    dev_pos: dict[str, pulp.LpVariable] = {}
    dev_neg: dict[str, pulp.LpVariable] = {}
    slew_pos: dict[str, pulp.LpVariable] = {}
    slew_neg: dict[str, pulp.LpVariable] = {}
    absorb_ids: list[str] = []
    discharge_cap_w: dict[str, float] = {}
    """Discharge within the plan (incl. follow headroom): the load target."""
    beyond_cost = 0.0

    for inp in inputs:
        did = inp.device_id
        max_c = max(0.0, inp.max_charge_w)
        max_d = max(0.0, inp.max_discharge_w)
        is_charge = inp.planned_w > 1.0
        is_discharge = inp.planned_w < -1.0
        # Grid charging only where the plan asks for it and the device and
        # intent allow it; everything else charges from surplus only.
        grid_charge = is_charge and not inp.no_grid_charge

        c_pv[did] = pulp.LpVariable(f"c_pv__{did}", lowBound=0.0, upBound=max_c)
        c_grid[did] = pulp.LpVariable(
            f"c_grid__{did}", lowBound=0.0, upBound=max_c if grid_charge else 0.0
        )
        c[did] = c_pv[did] + c_grid[did]
        d[did] = pulp.LpVariable(f"d__{did}", lowBound=0.0, upBound=max_d)
        u[did] = pulp.LpVariable(f"u__{did}", cat="Binary")
        dev_pos[did] = pulp.LpVariable(f"dev_pos__{did}", lowBound=0.0)
        dev_neg[did] = pulp.LpVariable(f"dev_neg__{did}", lowBound=0.0)
        slew_pos[did] = pulp.LpVariable(f"slew_pos__{did}", lowBound=0.0)
        slew_neg[did] = pulp.LpVariable(f"slew_neg__{did}", lowBound=0.0)

        prob += c[did] <= max_c * u[did], f"charge_mode__{did}"
        prob += d[did] <= max_d * (1 - u[did]), f"discharge_mode__{did}"

        # Only a discharge intent may discharge — idle and charge intents
        # (incl. PV-only charging without surplus) hold the stored energy.
        if not is_discharge:
            discharge_cap_w[did] = 0.0
        elif inp.export_allowed:
            discharge_cap_w[did] = max_d
        else:
            # Follow the live load, but within the plan's discharge budget.
            discharge_cap_w[did] = min(max_d, -inp.planned_w * DISCHARGE_FOLLOW_HEADROOM)
        value_w = _delivered_value(inp)
        if (
            is_discharge and not inp.export_allowed and value_w is not None
            and imp_p > value_w + FOLLOW_VALUE_MARGIN_EUR_PER_KWH
            and max_d > discharge_cap_w[did]
        ):
            # Unforecast load is worth covering (see "Plan fidelity"): up to
            # the device limit, the part beyond the headroom priced at what
            # the stored energy is worth to the plan.
            beyond = pulp.LpVariable(f"beyond__{did}", lowBound=0.0)
            prob += d[did] <= max_d, f"discharge_cap__{did}"
            prob += beyond >= d[did] - discharge_cap_w[did], f"beyond_plan__{did}"
            # The beyond part is priced at its value instead of the plan
            # tracking tie-breaker it also incurs (that would add up to
            # 0.05 €/kWh and keep the battery capped near break-even).
            beyond_cost += (value_w - DISCHARGE_TRACK_EUR_PER_KWH) * beyond / 1000.0
        else:
            prob += d[did] <= discharge_cap_w[did], f"discharge_cap__{did}"

        # A grid-charge intent delivers its planned power, within the
        # device's current limits (see "Plan fidelity" above).
        if grid_charge:
            prob += c[did] >= min(max_c, inp.planned_w), f"grid_charge_plan__{did}"

        # Every device except an export-allowed discharge absorbs surplus
        # that no plan uses, instead of letting it be exported.
        if not (is_discharge and inp.export_allowed):
            absorb_ids.append(did)

        # Track current plan power.  Platform sign convention: charge
        # positive, discharge negative.
        prob += (
            c[did] - d[did] - inp.planned_w == dev_pos[did] - dev_neg[did],
            f"plan_deviation__{did}",
        )

        # Penalize abrupt changes to avoid 0W↔XW chatter near grid zero-crossings.
        prob += (
            c[did] - d[did] - inp.prev_setpoint_w == slew_pos[did] - slew_neg[did],
            f"setpoint_slew__{did}",
        )

    # PV surplus is one pool shared by all batteries.
    prob += pulp.lpSum(c_pv.values()) <= surplus_w, "pv_pool"

    # Surplus no plan uses is absorbed rather than exported.  Charge
    # shortfalls are priced far above export revenue, so the plans are served
    # from the pool first and this only binds for the remainder.
    if absorb_ids and surplus_w > 1.0:
        absorb_cap_w = sum(max(0.0, inp.max_charge_w) for inp in inputs if inp.device_id in absorb_ids)
        if absorb_cap_w > 0.0:
            prob += (
                pulp.lpSum(c_pv[did] for did in absorb_ids) >= min(surplus_w, absorb_cap_w),
                "absorb_surplus",
            )

    g_imp = pulp.LpVariable("g_imp", lowBound=0.0)
    g_exp = pulp.LpVariable("g_exp", lowBound=0.0)

    prob += (
        g_imp - g_exp
        == grid_power_w + pulp.lpSum(c.values()) - pulp.lpSum(d.values()),
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
        # Discharge covers the house load only: neither export nor feeding
        # another battery's (grid) charging.
        prob += (
            pulp.lpSum(d.values()) <= max(0.0, grid_power_w + import_buffer_w),
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
        total_cap_w = sum(discharge_cap_w[inp_id] for inp_id in meet_load_discharge_ids)
        target_discharge_w = min(max(0.0, grid_power_w), total_cap_w)
        unmet_import_w = pulp.LpVariable("unmet_import_w", lowBound=0.0)
        prob += (
            pulp.lpSum(d[inp_id] for inp_id in meet_load_discharge_ids) + unmet_import_w
            >= target_discharge_w,
            "meet_load_discharge_target",
        )

    grid_cost = (imp_p * g_imp - exp_p * g_exp) / 1000.0

    # Plan tracking.  Charge intents: a shortfall is priced lexicographically
    # (PLAN_SHORTFALL_EUR_PER_KWH); idle/charge over-delivery (absorbing
    # surplus) and the slew term are small tie-breakers.
    track_cost = 0.0
    slew_cost = 0.0
    for inp in inputs:
        did = inp.device_id
        if inp.planned_w < -1.0:
            track_cost += DISCHARGE_TRACK_EUR_PER_KWH * (dev_pos[did] + dev_neg[did]) / 1000.0
            slew_cost += 0.015 * (slew_pos[did] + slew_neg[did]) / 1000.0
        else:
            under_w = PLAN_SHORTFALL_EUR_PER_KWH if inp.planned_w > 1.0 else 0.2
            track_cost += (0.2 * dev_pos[did] + under_w * dev_neg[did]) / 1000.0
            slew_cost += 0.08 * (slew_pos[did] + slew_neg[did]) / 1000.0

    unmet_cost = 0.0 if unmet_import_w is None else 0.6 * unmet_import_w / 1000.0
    prob += grid_cost + track_cost + slew_cost + unmet_cost + beyond_cost, "objective"

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

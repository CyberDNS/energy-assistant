"""Savings accounting — what Energy Assistant saves against an unplanned site.

Concept
-------
Two worlds are compared, interval by interval, on identical inflexible load,
PV and prices:

* the **real** site, where Energy Assistant controls batteries and EVs, and
* a **shadow** site that runs unplanned: batteries do plain self-consumption
  (charge from surplus, discharge to cover load, regulated at the grid
  connection point) and a car charges at full power from the moment it is
  plugged in.

    saving = shadow cost − real cost + Δ inventory value

The inventory term keeps the comparison fair while the two worlds hold
different amounts of energy.  A battery that was grid-charged an hour ago has
cost money but not yet earned any; a car whose charge was deferred to the
cheap night hours has not been paid for yet.  The difference in stored energy
is therefore valued — batteries at the ledger cost basis, a car's missing
energy at the price the shadow car paid for it — and only the *change* of
that value is booked.  Summed over any period the term telescopes to the
value difference at the two ends, so it never invents or loses money.

The same three pure functions price all three numbers the KPIs compare:

* ``SavingsTracker.tick``  — real saving, from live measurements,
* ``predict_slots``        — predicted saving, from a solved ``EnergyPlan``,
* ``settle``               — the tariff rules both of them use.

Known simplifications
---------------------
* Threshold devices (coolers, …) count as fixed load in both worlds, so they
  contribute no saving.
* Battery degradation is not real money and is left out.
* The shadow battery uses the configured efficiencies; a real battery that
  performs worse than configured shows up as a (small) negative saving.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .models import EnergyPlan, StorageConstraints

# Live samples further apart than this are not bridged: the time in between
# is left out of both worlds instead of being priced with a stale reading.
_MAX_GAP_S = 120.0


# ---------------------------------------------------------------------------
# Tariff settlement
# ---------------------------------------------------------------------------


def settle(
    primary_net_kwh: float,
    secondary: Sequence[tuple[float, float]],
    import_price: float,
    export_price: float,
) -> float:
    """Cost (€) of one interval under the two-zone tariff rules.

    primary_net_kwh:
        Net energy of the primary metering zone: positive = import, negative
        = surplus.  Must not contain the separately billed loads.
    secondary:
        ``(energy_kwh, price_eur_per_kwh)`` per separately billed load (e.g.
        a heat pump on its own tariff).  Primary surplus feeds these loads
        first — most expensive first — and only the rest is exported.

    Mirrors the optimizer's formulation (see ``plugins/milp_highs``).
    """
    cost = max(0.0, primary_net_kwh) * import_price
    surplus = max(0.0, -primary_net_kwh)
    for load, price in sorted(secondary, key=lambda lp: -lp[1]):
        used = min(max(0.0, load), surplus)
        surplus -= used
        cost += (max(0.0, load) - used) * price
    return cost - surplus * export_price


# ---------------------------------------------------------------------------
# Shadow (unplanned) site state
# ---------------------------------------------------------------------------


@dataclass
class ShadowEv:
    """One charging session, real car vs. the car that charges immediately."""

    real_kwh: float = 0.0
    """Wall energy the real car received this session."""
    shadow_kwh: float = 0.0
    """Wall energy the unplanned car would have received by now."""
    need_kwh: float = 0.0
    """Total wall energy the unplanned car charges this session."""
    shadow_cost: float = 0.0
    """Σ shadow energy × import price — values the energy the real car lacks."""

    @property
    def value_eur(self) -> float:
        """Value of the real car's energy lead (negative: it is behind)."""
        if self.shadow_kwh <= 0.0:
            return 0.0
        return (self.real_kwh - self.shadow_kwh) * self.shadow_cost / self.shadow_kwh


@dataclass
class ShadowState:
    """Everything the unplanned site carries from one interval to the next."""

    stored_kwh: dict[str, float] = field(default_factory=dict)
    """Stored energy of each shadow battery, keyed by device id."""
    evs: dict[str, ShadowEv] = field(default_factory=dict)
    """Open charging sessions, keyed by chargepoint device id."""
    closed_ev_value: float = 0.0
    """Summed ``value_eur`` of finished sessions.  Kept so that unplugging a
    car never makes the inventory value jump."""

    def copy(self) -> "ShadowState":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ShadowState":
        return cls(
            stored_kwh={k: float(v) for k, v in (data.get("stored_kwh") or {}).items()},
            evs={k: ShadowEv(**v) for k, v in (data.get("evs") or {}).items()},
            closed_ev_value=float(data.get("closed_ev_value") or 0.0),
        )


@dataclass(frozen=True)
class EvStep:
    """What one chargepoint did during an interval."""

    plugged: bool | None
    """``None`` = unknown (e.g. connection lost): the session is left as is."""
    real_kwh: float = 0.0
    max_kw: float = 0.0


def set_ev_need(state: ShadowState, device_id: str, remaining_kwh: float) -> None:
    """Tell the shadow car how much wall energy the session still needs.

    Called whenever the charging goal is re-evaluated.  The unplanned car
    charges the same total energy as the real one — what the real car got so
    far plus what it is still going to get — just as early as possible.
    """
    ev = state.evs.setdefault(device_id, ShadowEv())
    ev.need_kwh = ev.real_kwh + max(0.0, remaining_kwh)


def _ev_step(
    state: ShadowState, device_id: str, step: EvStep, hours: float, import_price: float
) -> float:
    """Advance one chargepoint; return the shadow car's energy this interval."""
    if step.plugged is None:
        return 0.0
    ev = state.evs.get(device_id)
    if not step.plugged:
        if ev is not None:
            state.closed_ev_value += ev.value_eur
            del state.evs[device_id]
        return 0.0
    if ev is None:
        ev = state.evs[device_id] = ShadowEv()
    ev.real_kwh += step.real_kwh
    # Never behind the real car: without a goal the shadow simply follows it.
    ev.need_kwh = max(ev.need_kwh, ev.real_kwh)
    charged = min(
        max(0.0, ev.need_kwh - ev.shadow_kwh),
        max(step.max_kw * hours, step.real_kwh),
    )
    ev.shadow_kwh += charged
    ev.shadow_cost += charged * import_price
    return charged


def _self_consumption(
    stored: dict[str, float],
    batteries: Iterable[StorageConstraints],
    site_net_kwh: float,
    hours: float,
) -> float:
    """Run the shadow batteries against the site's net load for one interval.

    Returns their net AC energy (positive = charging) and updates *stored*.
    Batteries are used in configuration order.
    """
    net = 0.0
    remaining = abs(site_net_kwh)
    for sc in batteries:
        if remaining <= 1e-9:
            break
        energy = stored.get(sc.device_id)
        if energy is None:
            continue
        if site_net_kwh < 0:  # surplus → charge
            room = max(0.0, sc.capacity_kwh * sc.max_soc_pct / 100.0 - energy)
            take = min(remaining, sc.max_charge_kw * hours, room / sc.charge_efficiency)
            stored[sc.device_id] = energy + take * sc.charge_efficiency
            remaining -= take
            net += take
        else:  # deficit → discharge
            usable = max(0.0, energy - sc.capacity_kwh * sc.min_soc_pct / 100.0)
            give = min(remaining, sc.max_discharge_kw * hours, usable * sc.discharge_efficiency)
            stored[sc.device_id] = energy - give / sc.discharge_efficiency
            remaining -= give
            net -= give
    return net


def baseline_step(
    state: ShadowState,
    batteries: Sequence[StorageConstraints],
    *,
    residual_kwh: float,
    secondary: Sequence[tuple[float, float]],
    ev: Mapping[str, EvStep],
    import_price: float,
    export_price: float,
    hours: float,
) -> float:
    """Advance the unplanned site by one interval and return its cost (€).

    residual_kwh:
        Site net energy without any battery or EV: all inflexible load
        (separately billed loads included) minus PV.
    """
    ev_kwh = sum(
        _ev_step(state, device_id, step, hours, import_price)
        for device_id, step in ev.items()
    )
    site = residual_kwh + ev_kwh
    battery_kwh = _self_consumption(state.stored_kwh, batteries, site, hours)
    secondary_total = sum(max(0.0, load) for load, _ in secondary)
    return settle(site + battery_kwh - secondary_total, secondary, import_price, export_price)


def inventory_value(
    stored_kwh: Mapping[str, float],
    state: ShadowState,
    cost_basis: Mapping[str, float],
) -> float:
    """Value (€) of the energy the compared site holds beyond the shadow site.

    stored_kwh:
        Stored energy per battery in the compared (real or planned) site.
    """
    value = state.closed_ev_value + sum(ev.value_eur for ev in state.evs.values())
    for device_id, shadow in state.stored_kwh.items():
        value += (stored_kwh.get(device_id, shadow) - shadow) * cost_basis.get(device_id, 0.0)
    return value


# ---------------------------------------------------------------------------
# Real saving — from live measurements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvLive:
    plugged: bool | None
    power_kw: float
    max_kw: float


@dataclass(frozen=True)
class LiveSample:
    """One consistent snapshot of the real site."""

    timestamp: datetime
    site_net_kw: float
    """Grid connection point: positive = import, negative = export."""
    storage_kw: Mapping[str, float]
    """AC power per battery: positive = charging."""
    stored_kwh: Mapping[str, float]
    """Stored energy per battery."""
    cost_basis: Mapping[str, float]
    ev: Mapping[str, EvLive]
    secondary: Sequence[tuple[float, float]]
    """``(power_kw, price_eur_per_kwh)`` per separately billed load."""
    import_price: float
    export_price: float


@dataclass
class ActualSlot:
    """Measured result of one accounting slot."""

    start: datetime
    hours: float = 0.0
    """Time actually covered by valid measurements (≤ slot length)."""
    baseline_cost: float = 0.0
    actual_cost: float = 0.0
    inventory_delta: float = 0.0

    @property
    def saving(self) -> float:
        return self.baseline_cost - self.actual_cost + self.inventory_delta


def slot_start(ts: datetime, step_minutes: int) -> datetime:
    """Start (UTC) of the accounting slot *ts* falls into."""
    ts = ts.astimezone(timezone.utc)
    minutes = ts.hour * 60 + ts.minute
    floored = minutes - minutes % step_minutes
    return ts.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


class SavingsTracker:
    """Integrates the real and the shadow site over fixed accounting slots.

    Feed it one ``LiveSample`` per control tick; whenever a slot is complete
    ``tick`` returns it.  Ticks without a usable sample are simply not fed —
    that time is then missing from both worlds alike.
    """

    def __init__(
        self,
        batteries: Sequence[StorageConstraints],
        step_minutes: int,
        state: ShadowState | None = None,
    ) -> None:
        self._batteries = list(batteries)
        self._step_minutes = step_minutes
        self.state = state or ShadowState()
        self._slot: ActualSlot | None = None
        self._last_ts: datetime | None = None
        self._last_value = 0.0

    def tick(self, sample: LiveSample) -> list[ActualSlot]:
        """Book the time since the previous sample; return finished slots.

        An interval belongs to the slot it starts in and is priced with the
        readings of the sample that ends it.
        """
        # A battery the shadow site has not seen yet starts level with reality.
        for sc in self._batteries:
            if sc.device_id not in self.state.stored_kwh and sc.device_id in sample.stored_kwh:
                self.state.stored_kwh[sc.device_id] = sample.stored_kwh[sc.device_id]

        previous, self._last_ts = self._last_ts, sample.timestamp
        gap_s = (sample.timestamp - previous).total_seconds() if previous else None
        if previous is None or gap_s <= 0 or gap_s > _MAX_GAP_S:
            # Nothing to bridge.  Restart the inventory reference so whatever
            # happened unobserved is not booked as a saving or a loss.
            self._last_value = inventory_value(sample.stored_kwh, self.state, sample.cost_basis)
            return self._roll(sample.timestamp)

        if self._slot is None:
            self._slot = ActualSlot(start=slot_start(previous, self._step_minutes))
        hours = gap_s / 3600.0
        secondary = [(max(0.0, kw) * hours, price) for kw, price in sample.secondary]
        secondary_total = sum(load for load, _ in secondary)
        ev_steps = {
            device_id: EvStep(
                plugged=ev.plugged,
                real_kwh=max(0.0, ev.power_kw) * hours if ev.plugged else 0.0,
                max_kw=ev.max_kw,
            )
            for device_id, ev in sample.ev.items()
        }
        site_kwh = sample.site_net_kw * hours
        flexible_kwh = sum(sample.storage_kw.values()) * hours + sum(
            step.real_kwh for step in ev_steps.values()
        )

        slot = self._slot
        slot.hours += hours
        slot.actual_cost += settle(
            site_kwh - secondary_total, secondary, sample.import_price, sample.export_price
        )
        slot.baseline_cost += baseline_step(
            self.state,
            self._batteries,
            residual_kwh=site_kwh - flexible_kwh,
            secondary=secondary,
            ev=ev_steps,
            import_price=sample.import_price,
            export_price=sample.export_price,
            hours=hours,
        )
        value = inventory_value(sample.stored_kwh, self.state, sample.cost_basis)
        slot.inventory_delta += value - self._last_value
        self._last_value = value
        return self._roll(sample.timestamp)

    def _roll(self, now: datetime) -> list[ActualSlot]:
        """Close the open slot once *now* has left it."""
        slot = self._slot
        if slot is None or slot.start == slot_start(now, self._step_minutes):
            return []
        self._slot = None
        return [slot] if slot.hours > 0 else []


# ---------------------------------------------------------------------------
# Predicted saving — from a solved plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictedSlot:
    """Predicted result of one plan slot."""

    start: datetime
    hours: float
    baseline_cost: float
    plan_cost: float
    inventory_delta: float

    @property
    def saving(self) -> float:
        return self.baseline_cost - self.plan_cost + self.inventory_delta


def predict_slots(
    plan: EnergyPlan,
    state: ShadowState,
    batteries: Sequence[StorageConstraints],
    *,
    stored_kwh: Mapping[str, float],
    cost_basis: Mapping[str, float],
    ev_max_kw: Mapping[str, float],
) -> list[PredictedSlot]:
    """Predicted saving per slot of *plan*, continuing from the shadow *state*.

    The plan supplies the planned site (``PlanFlow``); the unplanned site is
    simulated forward from *state* on the plan's own forecasts and prices.
    *state* is not modified.  Returns ``[]`` for plans that carry no
    economics (no solve, or an optimizer that does not report them).

    stored_kwh:
        Real stored energy per battery right now (the plan's starting point).
    """
    sim = state.copy()
    for sc in batteries:
        if sc.device_id in stored_kwh:
            sim.stored_kwh.setdefault(sc.device_id, stored_kwh[sc.device_id])
    value = inventory_value(stored_kwh, sim, cost_basis)

    out: list[PredictedSlot] = []
    for flow in plan.flows:
        if flow.load_kw is None or not flow.hours or flow.import_price is None:
            return []
        hours = flow.hours
        export_price = flow.export_price or 0.0
        secondary = [
            (kw * hours, price)
            for kw, price in zip(flow.secondary_kw, flow.secondary_prices)
        ]
        secondary_total = sum(load for load, _ in secondary)
        fixed_kwh = (flow.load_kw - flow.pv_kw) * hours

        # Every open session stays plugged in; the planned charging is what
        # the "real" car receives.
        ev_steps = {
            device_id: EvStep(
                plugged=True,
                real_kwh=flow.ev_kw.get(device_id, 0.0) * hours,
                max_kw=ev_max_kw.get(device_id, flow.ev_kw.get(device_id, 0.0)),
            )
            for device_id in {*sim.evs, *flow.ev_kw}
        }
        plan_cost = settle(
            fixed_kwh + (flow.storage_kw + sum(flow.ev_kw.values())) * hours,
            secondary,
            flow.import_price,
            export_price,
        )
        baseline_cost = baseline_step(
            sim,
            batteries,
            residual_kwh=fixed_kwh + secondary_total,
            secondary=secondary,
            ev=ev_steps,
            import_price=flow.import_price,
            export_price=export_price,
            hours=hours,
        )
        new_value = inventory_value(flow.stored_kwh, sim, cost_basis)
        out.append(
            PredictedSlot(
                start=flow.timestep,
                hours=hours,
                baseline_cost=baseline_cost,
                plan_cost=plan_cost,
                inventory_delta=new_value - value,
            )
        )
        value = new_value
    return out


def day_ahead_window_end(now: datetime, lead: timedelta) -> datetime:
    """End (exclusive) of the slots a plan made at *now* may freeze.

    A day's prediction is frozen by the last plans before local midnight and
    then left alone, so it is a genuine forecast of the whole coming day.
    Plans made earlier in a day may only fill slots of that same day that
    have no prediction yet (after a restart or a failed planning run).

    lead:
        How long before midnight the coming day opens up — one planning
        interval plus one slot, so at least one plan falls inside it.
    """
    day = (now + lead).astimezone().date() + timedelta(days=1)
    # A naive datetime is read as local time, which keeps midnight correct
    # across daylight-saving changes.
    return datetime(day.year, day.month, day.day).astimezone(timezone.utc)

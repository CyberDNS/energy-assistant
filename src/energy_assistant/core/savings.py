"""Savings accounting — Energy Assistant vs. a simulated uncontrolled house.

Concept
-------
Two houses are compared slot by slot on identical measured household load,
PV and prices (see issue #82):

* the **real** house, where Energy Assistant controls batteries and EVs, and
* an **uncontrolled** house, simulated on the same numbers, where

  - PV surplus goes into the batteries first, then into a plugged-in car,
  - the batteries discharge to cover the load of the zone they regulate on
    (the default-tariff zone: household and cars, not separately billed
    loads such as a heat pump on its own tariff),
  - a car with a charge plan charges from the grid **as late as possible**
    to reach the plan's target by its deadline (PV-only plans: PV only;
    no plan: charges like the real car).

Both houses keep a **ledger** per battery: energy that is charged moves its
cost into the ledger (grid energy at the import price, PV at the feed-in
price it would otherwise have earned); energy that is used is costed at the
ledger's average cost.  So a slot's cost is what its consumption cost:

    cost = grid bill − cost moved into ledgers + ledger cost of energy used

    saving = uncontrolled cost − real cost

Charging slots therefore save nothing; the saving appears when the energy is
used: (price at that moment − ledger cost) × kWh.  Energy still stored is not
counted in either house until it is used.

Cars work the same way with a FIFO ledger per charging session: energy one
car got earlier than the other is held at its cost, and once both cars have
charged the same kWh the difference in what they paid for it is booked.
Energy the real car gets beyond what the uncontrolled car charges (or that is
still unmatched when the car is unplugged) is neutral.

Device behaviour both houses share
----------------------------------
* Battery power within ``StorageConstraints.standby_w`` is household load.
* A real battery recharging itself below its minimum SoC (inverter
  protection) is mirrored onto the uncontrolled battery.
* Real self-discharge and SoC jumps change the real ledger's kWh but not its
  value, so later uses of that battery's energy cost more — no correction
  needed.

Housekeeping
------------
The uncontrolled batteries are set to the real SoC (keeping their own average
cost) whenever both are at the same SoC limit, at least every
``RESYNC_AFTER``, and after data gaps longer than ``RESET_AFTER_GAP``.  None
of this is booked.  Car sessions start and end with the real plug state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .models import StorageConstraints

STATE_VERSION = 2

# Live samples further apart than this are not bridged: the time in between
# is left out of both houses instead of being priced with a stale reading.
_MAX_GAP_S = 120.0

RESET_AFTER_GAP = timedelta(hours=1)
"""After a data gap this long the uncontrolled house restarts level with
reality (nothing is booked for the gap)."""

RESYNC_AFTER = timedelta(days=3)
"""The uncontrolled batteries are set to the real SoC at least this often."""

LIMIT_BAND_PCT = 2.0
"""Two batteries within this many % of capacity of the same SoC limit count
as "both empty" / "both full" (natural resync point)."""


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


def slot_start(ts: datetime, step_minutes: int) -> datetime:
    """Start (UTC) of the accounting slot *ts* falls into."""
    ts = ts.astimezone(timezone.utc)
    minutes = ts.hour * 60 + ts.minute
    floored = minutes - minutes % step_minutes
    return ts.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Ledgers
# ---------------------------------------------------------------------------


@dataclass
class Ledger:
    """Stored energy of one battery and what it cost (average cost)."""

    stored_kwh: float = 0.0
    value_eur: float = 0.0

    @property
    def cost_per_kwh(self) -> float:
        return self.value_eur / self.stored_kwh if self.stored_kwh > 1e-9 else 0.0

    def add(self, stored_kwh: float, cost_eur: float) -> None:
        self.stored_kwh += stored_kwh
        self.value_eur += cost_eur

    def take(self, stored_kwh: float) -> float:
        """Remove *stored_kwh*; return their cost at the average cost."""
        if stored_kwh <= 0.0:
            return 0.0
        cost = self.cost_per_kwh * stored_kwh
        if self.value_eur > 0:
            cost = min(cost, self.value_eur)  # SoC readings may lag the flows
        self.value_eur -= cost
        self.stored_kwh = max(0.0, self.stored_kwh - stored_kwh)
        return cost

    def resize(self, stored_kwh: float) -> None:
        """Set the stored energy, keeping the average cost (housekeeping)."""
        avg = self.cost_per_kwh
        self.stored_kwh = max(0.0, stored_kwh)
        self.value_eur = avg * self.stored_kwh


@dataclass
class EvSession:
    """One charging session: the real car next to the uncontrolled one."""

    mode: str = "follow"
    """``deadline`` (charge plan: as late as possible), ``pv_only`` or
    ``follow`` (no plan: charge like the real car)."""
    need_kwh: float = 0.0
    """Total wall energy the uncontrolled car charges this session."""
    deadline: str | None = None
    """ISO time by which ``need_kwh`` must be in (``deadline`` mode)."""
    real_kwh: float = 0.0
    unc_kwh: float = 0.0
    real_queue: list[list[float]] = field(default_factory=list)
    """``[kWh, €]`` the real car charged that is not matched yet (FIFO)."""
    unc_queue: list[list[float]] = field(default_factory=list)

    @property
    def matched_kwh(self) -> float:
        return min(self.real_kwh, self.unc_kwh)

    def deadline_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.deadline) if self.deadline else None


def _release(queue: list[list[float]], kwh: float) -> float:
    """Remove *kwh* from the front of a FIFO ``[kWh, €]`` queue; return €."""
    cost = 0.0
    while kwh > 1e-12 and queue:
        q_kwh, q_cost = queue[0]
        take = min(kwh, q_kwh)
        part = q_cost * take / q_kwh if q_kwh > 0 else 0.0
        cost += part
        kwh -= take
        if take >= q_kwh - 1e-12:
            queue.pop(0)
        else:
            queue[0] = [q_kwh - take, q_cost - part]
    return cost


@dataclass
class SavingsState:
    """Everything the accounting carries from one tick to the next."""

    real: dict[str, Ledger] = field(default_factory=dict)
    """Accounting ledger of each real battery (plain average cost)."""
    unc: dict[str, Ledger] = field(default_factory=dict)
    """Simulated battery of the uncontrolled house (its state *is* its ledger)."""
    last_resync: dict[str, str] = field(default_factory=dict)
    evs: dict[str, EvSession] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"version": STATE_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SavingsState | None":
        """Restore a saved state; ``None`` for an older format."""
        if data.get("version") != STATE_VERSION:
            return None
        return cls(
            real={k: Ledger(**v) for k, v in (data.get("real") or {}).items()},
            unc={k: Ledger(**v) for k, v in (data.get("unc") or {}).items()},
            last_resync=dict(data.get("last_resync") or {}),
            evs={k: EvSession(**v) for k, v in (data.get("evs") or {}).items()},
        )


# ---------------------------------------------------------------------------
# Live samples and slot results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvLive:
    plugged: bool | None
    """``None`` = unknown (e.g. connection lost): the session is left as is."""
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
    """Stored energy per battery (from its SoC)."""
    ev: Mapping[str, EvLive]
    secondary: Sequence[tuple[float, float]]
    """``(power_kw, price_eur_per_kwh)`` per separately billed load."""
    import_price: float
    export_price: float
    cost_basis: Mapping[str, float] = field(default_factory=dict)
    """Only used to start a battery's ledgers (value = stored × basis)."""


@dataclass
class SavingsSlot:
    """Result of one accounting slot."""

    start: datetime
    hours: float = 0.0
    """Time actually covered by valid measurements (≤ slot length)."""
    real_cost: float = 0.0
    """What the real house's consumption cost (ledger-based)."""
    uncontrolled_cost: float = 0.0
    real_bill: float = 0.0
    """Plain grid bill of the real house (charging included)."""
    uncontrolled_bill: float = 0.0
    ev_saving: float = 0.0
    """Part of the saving booked for cars."""
    # What the batteries and cars did (AC kWh; battery + = charge, − = discharge)
    real_battery_kwh: float = 0.0
    uncontrolled_battery_kwh: float = 0.0
    real_ev_kwh: float = 0.0
    uncontrolled_ev_kwh: float = 0.0
    real_stored: dict[str, float] = field(default_factory=dict)
    """Stored energy per battery at the end of the slot (kWh)."""
    uncontrolled_stored: dict[str, float] = field(default_factory=dict)
    price_hours: float = 0.0
    """Σ import price × hours; see ``import_price``."""

    @property
    def saving(self) -> float:
        return self.uncontrolled_cost - self.real_cost

    @property
    def import_price(self) -> float | None:
        """Time-weighted import price of the slot."""
        return self.price_hours / self.hours if self.hours > 0 else None


@dataclass
class _HouseResult:
    bill: float
    cost: float
    ev_cost: dict[str, float]
    """Cost of each car's charged energy, moved into its session queue."""


def _house_costs(
    household_kwh: float,
    secondary: Sequence[tuple[float, float]],
    import_price: float,
    export_price: float,
    batteries: Mapping[str, StorageConstraints],
    ledgers: Mapping[str, Ledger],
    charge_kwh: Mapping[str, float],
    discharge_kwh: Mapping[str, float],
    ev_kwh: Mapping[str, float],
) -> _HouseResult:
    """Bill and ledger-based cost of one house for one interval.

    household_kwh:
        Default-tariff zone energy without batteries and cars (+ deficit,
        − PV surplus).
    charge_kwh / discharge_kwh:
        AC energy per battery (both ≥ 0).  The ledgers are updated.
    ev_kwh:
        Energy per car (≥ 0); its cost is returned, not booked.

    Battery energy covers the household first, then the cars.  Charging
    (batteries, and cars beyond what batteries cover) is priced at its
    marginal effect on the bill.
    """
    charged = sum(charge_kwh.values())
    discharged = sum(discharge_kwh.values())
    ev_total = sum(ev_kwh.values())
    net = household_kwh + charged - discharged + ev_total
    bill = settle(net, secondary, import_price, export_price)

    used_cost = 0.0
    for device_id, kwh in discharge_kwh.items():
        if kwh > 0:
            sc = batteries[device_id]
            used_cost += ledgers[device_id].take(kwh / sc.discharge_efficiency)

    to_house = min(discharged, max(0.0, household_kwh))
    to_ev = min(discharged - to_house, ev_total)
    ev_battery_cost = used_cost * to_ev / discharged if discharged > 0 else 0.0
    ev_grid = ev_total - to_ev

    flex = charged + ev_grid
    marginal = (
        bill - settle(net - flex, secondary, import_price, export_price) if flex > 1e-12 else 0.0
    )
    for device_id, kwh in charge_kwh.items():
        if kwh > 0:
            sc = batteries[device_id]
            ledgers[device_id].add(kwh * sc.charge_efficiency, marginal * kwh / flex)
    ev_cost = {
        device_id: (
            (marginal * ev_grid / flex if flex > 1e-12 else 0.0) + ev_battery_cost
        ) * kwh / ev_total
        for device_id, kwh in ev_kwh.items()
        if ev_total > 0
    }
    cost = bill - marginal + (used_cost - ev_battery_cost)
    return _HouseResult(bill=bill, cost=cost, ev_cost=ev_cost)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class SavingsTracker:
    """Runs both houses over fixed accounting slots.

    Feed it one ``LiveSample`` per control tick; whenever a slot is complete
    ``tick`` returns it.  Ticks without a usable sample are simply not fed —
    that time is then missing from both houses alike.
    """

    def __init__(
        self,
        batteries: Sequence[StorageConstraints],
        step_minutes: int,
        state: SavingsState | None = None,
    ) -> None:
        self._batteries = {sc.device_id: sc for sc in batteries}
        self._step_minutes = step_minutes
        self.state = state or SavingsState()
        self._slot: SavingsSlot | None = None
        self._last_ts: datetime | None = None

    # -- charge plans -------------------------------------------------------

    def set_ev_goal(
        self,
        device_id: str,
        remaining_kwh: float | None,
        deadline: datetime | None,
        pv_only: bool = False,
    ) -> None:
        """Tell the uncontrolled car what the real car's charge plan asks for.

        Called whenever the charging goal is re-evaluated, for plugged-in cars
        only.  ``remaining_kwh=None`` (no plan) lets the uncontrolled car
        charge like the real one.
        """
        ev = self.state.evs.setdefault(device_id, EvSession())
        if pv_only:
            ev.mode, ev.deadline = "pv_only", None
        elif remaining_kwh is None or deadline is None:
            ev.mode, ev.deadline = "follow", None
        else:
            ev.mode = "deadline"
            ev.deadline = deadline.astimezone(timezone.utc).isoformat()
            ev.need_kwh = ev.real_kwh + max(0.0, remaining_kwh)

    # -- live data ----------------------------------------------------------

    def stored(self) -> dict[str, dict[str, dict[str, float]]]:
        """Energy still stored per house and battery (kWh and ledger value)."""
        return {
            house: {
                device_id: {"kwh": led.stored_kwh, "value_eur": led.value_eur}
                for device_id, led in ledgers.items()
            }
            for house, ledgers in (("real", self.state.real), ("uncontrolled", self.state.unc))
        }

    def tick(self, sample: LiveSample) -> list[SavingsSlot]:
        """Book the time since the previous sample; return finished slots.

        An interval belongs to the slot it starts in and is priced with the
        readings of the sample that ends it.
        """
        self._init_batteries(sample)
        previous, self._last_ts = self._last_ts, sample.timestamp
        gap_s = (sample.timestamp - previous).total_seconds() if previous else None
        if previous is None or gap_s <= 0 or gap_s > _MAX_GAP_S:
            if previous is not None and gap_s > RESET_AFTER_GAP.total_seconds():
                self._reset(sample)
            self._sync_real(sample)
            return self._roll(sample.timestamp)

        if self._slot is None:
            self._slot = SavingsSlot(start=slot_start(previous, self._step_minutes))
        hours = gap_s / 3600.0
        slot = self._slot
        slot.hours += hours

        secondary = [(max(0.0, kw) * hours, price) for kw, price in sample.secondary]
        secondary_total = sum(load for load, _ in secondary)
        prices = (sample.import_price, sample.export_price)

        # ── Real house: measured flows ────────────────────────────────────
        charge: dict[str, float] = {}
        discharge: dict[str, float] = {}
        forced: dict[str, float] = {}
        flex_kwh = 0.0
        for device_id, sc in self._batteries.items():
            kw = sample.storage_kw.get(device_id, 0.0)
            if abs(kw) * 1000.0 <= sc.standby_w:
                continue  # standby draw: household load in both houses
            kwh = kw * hours
            flex_kwh += kwh
            if kwh > 0:
                charge[device_id] = kwh
                stored = sample.stored_kwh.get(device_id)
                if stored is not None and stored < sc.capacity_kwh * sc.min_soc_pct / 100.0 - 1e-6:
                    forced[device_id] = kwh
            else:
                discharge[device_id] = -kwh
        real_ev: dict[str, float] = {}
        for device_id, ev in sample.ev.items():
            if ev.plugged:
                real_ev[device_id] = max(0.0, ev.power_kw) * hours
                flex_kwh += real_ev[device_id]
        household = sample.site_net_kw * hours - flex_kwh - secondary_total

        real = _house_costs(
            household, secondary, *prices, self._batteries, self.state.real,
            charge, discharge, real_ev,
        )

        # ── Uncontrolled house: simulated flows ───────────────────────────
        self._update_sessions(sample)
        u_charge, u_discharge, u_ev = self._simulate(sample, household, forced, hours)
        unc = _house_costs(
            household, secondary, *prices, self._batteries, self.state.unc,
            u_charge, u_discharge, u_ev,
        )

        # ── Cars: book matched energy ─────────────────────────────────────
        ev_real_booked = ev_unc_booked = 0.0
        for device_id, ev in self.state.evs.items():
            before = ev.matched_kwh
            if real_ev.get(device_id, 0.0) > 0:
                ev.real_kwh += real_ev[device_id]
                ev.real_queue.append([real_ev[device_id], real.ev_cost.get(device_id, 0.0)])
            if u_ev.get(device_id, 0.0) > 0:
                ev.unc_kwh += u_ev[device_id]
                ev.unc_queue.append([u_ev[device_id], unc.ev_cost.get(device_id, 0.0)])
            matched = ev.matched_kwh - before
            if matched > 0:
                ev_real_booked += _release(ev.real_queue, matched)
                ev_unc_booked += _release(ev.unc_queue, matched)

        slot.real_bill += real.bill
        slot.uncontrolled_bill += unc.bill
        slot.real_cost += real.cost + ev_real_booked
        slot.uncontrolled_cost += unc.cost + ev_unc_booked
        slot.ev_saving += ev_unc_booked - ev_real_booked
        slot.real_battery_kwh += sum(charge.values()) - sum(discharge.values())
        slot.uncontrolled_battery_kwh += sum(u_charge.values()) - sum(u_discharge.values())
        slot.real_ev_kwh += sum(real_ev.values())
        slot.uncontrolled_ev_kwh += sum(u_ev.values())
        slot.price_hours += sample.import_price * hours

        self._sync_real(sample)
        self._housekeeping(
            sample.timestamp, active={*charge, *discharge, *u_charge, *u_discharge}
        )
        slot.real_stored = {d: led.stored_kwh for d, led in self.state.real.items()}
        slot.uncontrolled_stored = {d: led.stored_kwh for d, led in self.state.unc.items()}
        return self._roll(sample.timestamp)

    # -- helpers ------------------------------------------------------------

    def _init_batteries(self, sample: LiveSample) -> None:
        """A battery seen for the first time starts with the same ledger in
        both houses."""
        for device_id in self._batteries:
            stored = sample.stored_kwh.get(device_id)
            if stored is None:
                continue
            if device_id not in self.state.real:
                basis = sample.cost_basis.get(device_id, 0.0)
                self.state.real[device_id] = Ledger(stored, stored * basis)
            if device_id not in self.state.unc:
                led = self.state.real[device_id]
                self.state.unc[device_id] = Ledger(led.stored_kwh, led.value_eur)
                self.state.last_resync[device_id] = sample.timestamp.isoformat()

    def _sync_real(self, sample: LiveSample) -> None:
        """The real ledgers' kWh follow the SoC readings (value is kept)."""
        for device_id, led in self.state.real.items():
            stored = sample.stored_kwh.get(device_id)
            if stored is not None:
                led.stored_kwh = max(0.0, stored)

    def _update_sessions(self, sample: LiveSample) -> None:
        """Open a session at plug-in, drop it (unbooked rest) at unplug."""
        for device_id, ev in sample.ev.items():
            if ev.plugged is True:
                self.state.evs.setdefault(device_id, EvSession())
            elif ev.plugged is False:
                self.state.evs.pop(device_id, None)

    def _simulate(
        self,
        sample: LiveSample,
        household: float,
        forced: Mapping[str, float],
        hours: float,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """Flows of the uncontrolled house for one interval (AC kWh)."""
        charge: dict[str, float] = {}
        discharge: dict[str, float] = {}
        ev_kwh: dict[str, float] = {}
        led = self.state.unc

        def room(device_id: str) -> float:
            sc = self._batteries[device_id]
            full = sc.capacity_kwh * sc.max_soc_pct / 100.0
            pending = charge.get(device_id, 0.0) * sc.charge_efficiency
            return max(0.0, full - led[device_id].stored_kwh - pending) / sc.charge_efficiency

        def charge_headroom(device_id: str) -> float:
            sc = self._batteries[device_id]
            return max(0.0, sc.max_charge_kw * hours - charge.get(device_id, 0.0))

        # Forced recharge below min SoC happens in both houses.
        for device_id, kwh in forced.items():
            if device_id in led:
                take = min(kwh, room(device_id))
                charge[device_id] = charge.get(device_id, 0.0) + take

        # PV surplus: batteries first (configuration order) …
        surplus = max(0.0, -household)
        for device_id in self._batteries:
            if device_id not in led or surplus <= 1e-12:
                continue
            take = min(surplus, charge_headroom(device_id), room(device_id))
            if take > 0:
                charge[device_id] = charge.get(device_id, 0.0) + take
                surplus -= take

        # … then the cars.
        end = sample.timestamp
        for device_id, ev in self.state.evs.items():
            live = sample.ev.get(device_id)
            if live is None or not live.plugged:
                continue
            need = ev.need_kwh if ev.mode == "deadline" else ev.real_kwh + max(
                0.0, live.power_kw * hours
            )
            missing = max(0.0, need - ev.unc_kwh)
            cap = live.max_kw * hours
            pv = min(surplus, cap, missing)
            surplus -= pv
            grid = 0.0
            rest = missing - pv
            if ev.mode == "deadline" and rest > 0:
                deadline = ev.deadline_dt()
                left_h = max(0.0, (deadline - end).total_seconds() / 3600.0) if deadline else 0.0
                grid = min(cap - pv, max(0.0, rest - live.max_kw * left_h))
            elif ev.mode == "follow" and rest > 0:
                grid = min(cap - pv, rest)
            if pv + grid > 0:
                ev_kwh[device_id] = pv + grid

        # Deficit of the zone (household + cars): batteries discharge.
        deficit = household + sum(charge.values()) + sum(ev_kwh.values())
        for device_id, sc in self._batteries.items():
            if device_id not in led or deficit <= 1e-12 or charge.get(device_id, 0.0) > 0:
                continue
            usable = max(0.0, led[device_id].stored_kwh - sc.capacity_kwh * sc.min_soc_pct / 100.0)
            give = min(deficit, sc.max_discharge_kw * hours, usable * sc.discharge_efficiency)
            if give > 0:
                discharge[device_id] = give
                deficit -= give
        return charge, discharge, ev_kwh

    def _housekeeping(self, now: datetime, active: set[str] | frozenset[str] = frozenset()) -> None:
        """Set uncontrolled batteries to the real SoC at natural resync points
        (both idle at the same SoC limit) and at least every ``RESYNC_AFTER``.

        *active*: batteries that charged or discharged this tick in either
        house — one just leaving a limit must not be pulled back to it, and the
        other house must not be handed energy the real battery is just storing.
        """
        for device_id, sc in self._batteries.items():
            real, unc = self.state.real.get(device_id), self.state.unc.get(device_id)
            if real is None or unc is None:
                continue
            band = sc.capacity_kwh * LIMIT_BAND_PCT / 100.0
            low = sc.capacity_kwh * sc.min_soc_pct / 100.0 + band
            high = sc.capacity_kwh * sc.max_soc_pct / 100.0 - band
            idle = device_id not in active
            both_empty = idle and real.stored_kwh <= low and unc.stored_kwh <= low
            both_full = idle and real.stored_kwh >= high and unc.stored_kwh >= high
            last = self.state.last_resync.get(device_id)
            overdue = last is None or now - datetime.fromisoformat(last) >= RESYNC_AFTER
            if both_empty or both_full or overdue:
                unc.resize(real.stored_kwh)
                self.state.last_resync[device_id] = now.isoformat()

    def _reset(self, sample: LiveSample) -> None:
        """Restart the uncontrolled house level with reality (after a gap)."""
        self._sync_real(sample)
        for device_id, real in self.state.real.items():
            unc = self.state.unc.get(device_id)
            if unc is not None:
                unc.resize(real.stored_kwh)
                self.state.last_resync[device_id] = sample.timestamp.isoformat()
        for ev in self.state.evs.values():
            ev.unc_kwh = ev.real_kwh
            ev.real_queue.clear()
            ev.unc_queue.clear()

    def _roll(self, now: datetime) -> list[SavingsSlot]:
        """Close the open slot once *now* has left it."""
        slot = self._slot
        if slot is None or slot.start == slot_start(now, self._step_minutes):
            return []
        self._slot = None
        return [slot] if slot.hours > 0 else []


def day_ahead_window_end(now: datetime, lead: timedelta) -> datetime:
    """End (exclusive) of the local day a plan made at *now* plans ahead for.

    Within *lead* before local midnight this is the end of the coming day;
    otherwise the end of the current day.
    """
    day = (now + lead).astimezone().date() + timedelta(days=1)
    # A naive datetime is read as local time, which keeps midnight correct
    # across daylight-saving changes.
    return datetime(day.year, day.month, day.day).astimezone(timezone.utc)

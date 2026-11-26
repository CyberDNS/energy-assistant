"""Savings accounting — Nornwatt vs. a simulated uncontrolled house.

Concept
-------
Two houses are compared slot by slot on identical measured household load,
PV and prices (see issue #82):

* the **real** house, where Nornwatt controls batteries and EVs, and
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
Energy the real car gets beyond what the uncontrolled car charges is neutral.

A car unplugged before its plan's deadline ends the session early.  The
uncontrolled car is then recalculated backwards from the unplug moment: it
charges the energy the real car got (up to the plan's target) as late as
possible before unplugging, at full power, in the slots it had not used yet.
Those slots are priced at their measured import price and corrected
afterwards (``SavingsTracker.pop_corrections``), so the saving shows where
the uncontrolled car would have charged.  This ignores that the uncontrolled
battery or PV might have covered part of that energy.

Device behaviour both houses share
----------------------------------
* Battery power within ``StorageConstraints.standby_w`` is household load.
* A battery with ``own_meter: grid_connection`` (an SMA whose Energy Meter
  sits at the grid connection) charges in the uncontrolled house only from
  surplus left at that point: PV a separately billed load such as the heat
  pump uses is not available to it.
* A real battery recharging itself below its minimum SoC (inverter
  protection) is mirrored onto the uncontrolled battery.
* Both houses run on the same battery efficiency: the learned one once it is
  in use (``efficiency: learned`` and a confident estimate), else the
  configured one.  The real house's losses are what the SoC readings show,
  so the uncontrolled house must not run on a better battery than it.
* Real self-discharge and SoC jumps change the real ledger's kWh but not its
  value, so later uses of that battery's energy cost more — no correction
  needed.

Threshold devices
-----------------
A device that keeps a measured value between two thresholds (aquarium
cooler, fridge, …) runs in the uncontrolled house like a plain thermostat:
on at one threshold, off at the other (respecting its minimum run and off
times), on the device's thermal model (``core/thermal_model``, learned or
configured).  Its energy is ordinary load there, so PV covers it before the
batteries charge.  Like the cars, both houses' device energy is matched
FIFO and the price difference is booked once both have used the same kWh.
The simulated value is set back to the measured one every
``DEVICE_RESYNC_AFTER`` (and after data gaps); energy still unmatched then is
neutral.

Power charges
-------------
With power-dependent charges on the default tariff (``core/power_charge``)
each house has its own 15-minute meter at the grid connection.  When an
interval ends, its threshold charge and what it added to the house's month
peak (``eur_per_kw_month`` per kW above the previous peak and the minimum)
are booked into the slot it ends in, in both the bill and the cost.  The
difference is the ``power_charges`` component.  The fee on the minimum and
on a peak both houses reach is the same in both and so not part of it.

Every slot also records the saving per component — ``batteries``, each car
and each device — so the dashboard can explain the total.

Stored energy
-------------
Because stored energy only counts when it is used, a house holding more of
it looks worse until it uses it.  Each slot therefore also records both
houses' ledgers at its end, and ``stored_position`` values the energy above
the minimum SoC as the saving it would make if used now: discharged energy
at the reference price (the mean import price of the last
``REFERENCE_PRICE_HOURS``) minus its ledger cost.  The real minus the
uncontrolled value is the saving still in the batteries; its change over a
window can be added to the booked saving (the API's ``real_with_stored``).

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

from .models import StorageConstraints, ThresholdConstraints
from .power_charge import POWER_COMPONENT, PowerChargeMeter, PowerCharges
from .thermal_model import ThermalModel, device_mode

STATE_VERSION = 2

# Live samples further apart than this are not bridged: the time in between
# is left out of both houses instead of being priced with a stale reading.
_MAX_GAP_S = 120.0

RESET_AFTER_GAP = timedelta(hours=1)
"""After a data gap this long the uncontrolled house restarts level with
reality (nothing is booked for the gap)."""

RESYNC_AFTER = timedelta(days=3)
"""The uncontrolled batteries are set to the real SoC at least this often."""

DEVICE_RESYNC_AFTER = timedelta(hours=24)
"""The simulated value of a threshold device is set back to the measured one
at least this often; unmatched device energy is neutral at that point."""

BATTERIES = "batteries"
"""Component key of the battery part of the saving."""

REFERENCE_PRICE_HOURS = 24.0
"""Stored energy is valued at the mean import price of this many hours."""

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
    plugged_at: str | None = None
    """ISO time the session started (plug-in)."""
    unc_by_slot: dict[str, float] = field(default_factory=dict)
    """Energy the uncontrolled car charged per slot (ISO start), so a
    recalculation at an early unplug knows which slots are still free."""

    @property
    def matched_kwh(self) -> float:
        return min(self.real_kwh, self.unc_kwh)

    def deadline_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.deadline) if self.deadline else None


@dataclass
class DeviceSim:
    """A threshold device: the uncontrolled thermostat next to the real one."""

    value: float | None = None
    """Simulated measured value of the uncontrolled device."""
    running: bool = False
    since: str | None = None
    """ISO time the uncontrolled device last switched (min run/off times)."""
    real_kwh: float = 0.0
    unc_kwh: float = 0.0
    real_queue: list[list[float]] = field(default_factory=list)
    unc_queue: list[list[float]] = field(default_factory=list)
    last_resync: str | None = None

    @property
    def matched_kwh(self) -> float:
        return min(self.real_kwh, self.unc_kwh)


def _dev(device_id: str) -> str:
    """Key of a threshold device among the consumers in ``_house_costs``."""
    return f"dev:{device_id}"


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
    devices: dict[str, DeviceSim] = field(default_factory=dict)
    power: dict[str, PowerChargeMeter] = field(default_factory=dict)
    """Power-charge meter per house (``real`` / ``unc``)."""

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
            devices={k: DeviceSim(**v) for k, v in (data.get("devices") or {}).items()},
            power={k: PowerChargeMeter.from_dict(v) for k, v in (data.get("power") or {}).items()},
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
class DeviceLive:
    """A threshold device right now; ``None`` = no usable reading."""

    power_kw: float | None
    value: float | None


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
    pv_kw: float | None = None
    """Measured PV production (≥ 0), ``None`` if unknown.  Caps the surplus
    the uncontrolled house can store: a household reading below zero without
    PV (e.g. grid meter and wallbox sampled at different moments while a car
    draws 11 kW) is measurement skew, not free energy."""
    devices: Mapping[str, DeviceLive] = field(default_factory=dict)
    """Threshold devices (see ``SavingsTracker`` *devices*)."""


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
    real_stored_value: dict[str, float] = field(default_factory=dict)
    """Ledger value per battery at the end of the slot (€)."""
    uncontrolled_stored_value: dict[str, float] = field(default_factory=dict)
    price_hours: float = 0.0
    """Σ import price × hours; see ``import_price``."""
    components: dict[str, float] = field(default_factory=dict)
    """Saving per component: ``batteries``, each car and each threshold
    device (by device id).  Sums to ``saving``."""
    evs: dict[str, dict[str, float]] = field(default_factory=dict)
    """Per car: ``real_kwh`` / ``unc_kwh`` charged in the slot."""
    devices: dict[str, dict[str, float | None]] = field(default_factory=dict)
    """Per threshold device: ``real_kwh`` / ``unc_kwh`` used and the measured
    (``real_value``) and simulated (``unc_value``) value at slot end."""

    @property
    def saving(self) -> float:
        return self.uncontrolled_cost - self.real_cost

    def add_component(self, key: str, saving: float) -> None:
        self.components[key] = self.components.get(key, 0.0) + saving

    @property
    def import_price(self) -> float | None:
        """Time-weighted import price of the slot."""
        return self.price_hours / self.hours if self.hours > 0 else None


@dataclass(frozen=True)
class SlotCorrection:
    """Change to an already closed slot (car recalculated at early unplug)."""

    start: datetime
    uncontrolled_ev_kwh: float
    uncontrolled_cost: float
    real_cost: float
    component: str = "ev"
    """Component the correction belongs to (the car's device id)."""

    @property
    def ev_saving(self) -> float:
        return self.uncontrolled_cost - self.real_cost


@dataclass
class _HouseResult:
    bill: float
    cost: float
    ev_cost: dict[str, float]
    """Cost of each car's charged energy, moved into its session queue."""
    net_kwh: float = 0.0
    """Net energy of the default-tariff zone (+ import, − surplus)."""


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
    return _HouseResult(bill=bill, cost=cost, ev_cost=ev_cost, net_kwh=net)


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
        devices: Sequence[ThresholdConstraints] = (),
        power_charges: PowerCharges | None = None,
    ) -> None:
        self._batteries = {sc.device_id: sc for sc in batteries}
        self._power = power_charges or PowerCharges()
        self._devices = {tc.device_id: tc for tc in devices}
        # Thermal model and current ambient per threshold device (the server
        # updates them from the learned models; configured rates until then).
        self._thermal: dict[str, tuple[ThermalModel, float | None]] = {
            tc.device_id: (ThermalModel.from_constraints(tc), None) for tc in devices
        }
        self._step_minutes = step_minutes
        self.state = state or SavingsState()
        self._slot: SavingsSlot | None = None
        self._last_ts: datetime | None = None
        # Import price of recent closed slots (for early-unplug recalculation)
        # and corrections to closed slots waiting to be persisted.
        self._prices: dict[datetime, float] = {}
        self._corrections: list[SlotCorrection] = []

    @property
    def batteries(self) -> list[StorageConstraints]:
        """The battery constraints both houses run on."""
        return list(self._batteries.values())

    def set_batteries(self, batteries: Sequence[StorageConstraints]) -> None:
        """Run both houses on new battery constraints — the learned
        efficiency once it is in use.  The ledgers are kept."""
        self._batteries = {sc.device_id: sc for sc in batteries}

    def pop_corrections(self) -> list[SlotCorrection]:
        """Corrections to already closed slots since the last call."""
        out, self._corrections = self._corrections, []
        return out

    def set_thermal_model(
        self, device_id: str, model: ThermalModel, ambient: float | None = None
    ) -> None:
        """Model (and current ambient) the uncontrolled thermostat runs on."""
        if device_id in self._devices:
            self._thermal[device_id] = (model, ambient)

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
        slot.price_hours += sample.import_price * hours

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
        real_dev: dict[str, float] = {}
        for device_id in self._devices:
            live = sample.devices.get(device_id)
            if live is not None and live.power_kw is not None:
                real_dev[device_id] = max(0.0, live.power_kw) * hours
                flex_kwh += real_dev[device_id]
        household = sample.site_net_kw * hours - flex_kwh - secondary_total

        real = _house_costs(
            household, secondary, *prices, self._batteries, self.state.real,
            charge, discharge, {**real_ev, **{_dev(d): e for d, e in real_dev.items()}},
        )

        # ── Uncontrolled house: simulated flows ───────────────────────────
        self._update_sessions(sample, previous)
        u_dev = self._step_devices(sample, previous, hours)
        u_charge, u_discharge, u_ev = self._simulate(
            sample, household, forced, hours, extra_load=sum(u_dev.values())
        )
        unc = _house_costs(
            household, secondary, *prices, self._batteries, self.state.unc,
            u_charge, u_discharge, {**u_ev, **{_dev(d): e for d, e in u_dev.items()}},
        )
        slot.add_component(BATTERIES, unc.cost - real.cost)

        # ── Power charges: each house's 15-minute import and month peak ──
        if self._power:
            charged: dict[str, float] = {}
            for house, res in (("real", real), ("unc", unc)):
                meter = self.state.power.setdefault(house, PowerChargeMeter())
                import_kw = max(0.0, res.net_kwh + secondary_total) / hours
                charged[house] = sum(
                    self._power.interval_cost(iv.import_kwh)
                    + self._power.peak_cost(iv.old_peak_kw, iv.new_peak_kw)
                    for iv in meter.add(previous, sample.timestamp, import_kw)
                )
            slot.real_bill += charged["real"]
            slot.uncontrolled_bill += charged["unc"]
            slot.real_cost += charged["real"]
            slot.uncontrolled_cost += charged["unc"]
            if charged["real"] or charged["unc"]:
                slot.add_component(POWER_COMPONENT, charged["unc"] - charged["real"])

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
                key = slot.start.isoformat()
                ev.unc_by_slot[key] = ev.unc_by_slot.get(key, 0.0) + u_ev[device_id]
            matched = ev.matched_kwh - before
            if matched > 0:
                r = _release(ev.real_queue, matched)
                u = _release(ev.unc_queue, matched)
                ev_real_booked += r
                ev_unc_booked += u
                slot.add_component(device_id, u - r)
        for device_id in {*real_ev, *u_ev}:
            d = slot.evs.setdefault(device_id, {"real_kwh": 0.0, "unc_kwh": 0.0})
            d["real_kwh"] += real_ev.get(device_id, 0.0)
            d["unc_kwh"] += u_ev.get(device_id, 0.0)

        # ── Threshold devices: book matched energy ────────────────────────
        dev_real_booked = dev_unc_booked = 0.0
        for device_id, sim in self.state.devices.items():
            if device_id not in self._devices:
                continue
            before = sim.matched_kwh
            if real_dev.get(device_id, 0.0) > 0:
                sim.real_kwh += real_dev[device_id]
                sim.real_queue.append([real_dev[device_id], real.ev_cost.get(_dev(device_id), 0.0)])
            if u_dev.get(device_id, 0.0) > 0:
                sim.unc_kwh += u_dev[device_id]
                sim.unc_queue.append([u_dev[device_id], unc.ev_cost.get(_dev(device_id), 0.0)])
            matched = sim.matched_kwh - before
            if matched > 0:
                r = _release(sim.real_queue, matched)
                u = _release(sim.unc_queue, matched)
                dev_real_booked += r
                dev_unc_booked += u
                slot.add_component(device_id, u - r)
            live = sample.devices.get(device_id)
            d = slot.devices.setdefault(device_id, {"real_kwh": 0.0, "unc_kwh": 0.0})
            d["real_kwh"] += real_dev.get(device_id, 0.0)
            d["unc_kwh"] += u_dev.get(device_id, 0.0)
            d["real_value"] = live.value if live is not None else None
            d["unc_value"] = sim.value

        slot.real_bill += real.bill
        slot.uncontrolled_bill += unc.bill
        slot.real_cost += real.cost + ev_real_booked + dev_real_booked
        slot.uncontrolled_cost += unc.cost + ev_unc_booked + dev_unc_booked
        slot.ev_saving += ev_unc_booked - ev_real_booked
        slot.real_battery_kwh += sum(charge.values()) - sum(discharge.values())
        slot.uncontrolled_battery_kwh += sum(u_charge.values()) - sum(u_discharge.values())
        slot.real_ev_kwh += sum(real_ev.values())
        slot.uncontrolled_ev_kwh += sum(u_ev.values())

        self._sync_real(sample)
        self._housekeeping(
            sample.timestamp, active={*charge, *discharge, *u_charge, *u_discharge}
        )
        self._resync_devices(sample)
        slot.real_stored = {d: led.stored_kwh for d, led in self.state.real.items()}
        slot.uncontrolled_stored = {d: led.stored_kwh for d, led in self.state.unc.items()}
        slot.real_stored_value = {d: led.value_eur for d, led in self.state.real.items()}
        slot.uncontrolled_stored_value = {d: led.value_eur for d, led in self.state.unc.items()}
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

    def _update_sessions(self, sample: LiveSample, previous: datetime) -> None:
        """Open a session at plug-in; close it at unplug."""
        for device_id, ev in sample.ev.items():
            if ev.plugged is True:
                session = self.state.evs.setdefault(device_id, EvSession())
                if session.plugged_at is None:
                    session.plugged_at = previous.isoformat()
            elif ev.plugged is False:
                session = self.state.evs.pop(device_id, None)
                if session is not None:
                    self._settle_early_unplug(
                        device_id, session, previous, ev.max_kw, sample.import_price
                    )

    def _settle_early_unplug(
        self,
        device_id: str,
        ev: EvSession,
        unplugged: datetime,
        max_kw: float,
        fallback_price: float,
    ) -> None:
        """Recalculate the uncontrolled car of a session that ended before its
        deadline: it charges the energy the real car got (up to the plan's
        target) as late as possible before *unplugged*.  Booked into the open
        slot or emitted as corrections for closed slots."""
        deadline = ev.deadline_dt()
        if ev.mode != "deadline" or deadline is None or unplugged >= deadline or max_kw <= 0:
            return
        unmatched_real = sum(q[0] for q in ev.real_queue)
        missing = min(ev.real_kwh, ev.need_kwh) - ev.unc_kwh
        if missing <= 1e-6 or unmatched_real <= 1e-9:
            return
        missing = min(missing, unmatched_real)
        real_cost_per_kwh = sum(q[1] for q in ev.real_queue) / unmatched_real
        plugged_at = (
            datetime.fromisoformat(ev.plugged_at) if ev.plugged_at else unplugged - timedelta(days=1)
        )

        known = [*self._prices.values()]
        if self._slot is not None and self._slot.import_price is not None:
            known.append(self._slot.import_price)
        default_price = sum(known) / len(known) if known else fallback_price

        # Walk back from the unplug moment through the slots, filling the
        # capacity the uncontrolled car did not use.
        t, rest = unplugged, missing
        while rest > 1e-9 and t > plugged_at:
            start = slot_start(t - timedelta(microseconds=1), self._step_minutes)
            lo = max(start, plugged_at)
            hours = (t - lo).total_seconds() / 3600.0
            free = max(0.0, max_kw * hours - ev.unc_by_slot.get(start.isoformat(), 0.0))
            kwh = min(rest, free)
            t = lo
            if kwh <= 0:
                continue
            rest -= kwh
            if self._slot is not None and start == self._slot.start:
                price = self._slot.import_price or default_price
                unc_cost, real_cost = kwh * price, kwh * real_cost_per_kwh
                self._slot.uncontrolled_ev_kwh += kwh
                self._slot.uncontrolled_cost += unc_cost
                self._slot.uncontrolled_bill += unc_cost
                self._slot.real_cost += real_cost
                self._slot.ev_saving += unc_cost - real_cost
                self._slot.add_component(device_id, unc_cost - real_cost)
                d = self._slot.evs.setdefault(device_id, {"real_kwh": 0.0, "unc_kwh": 0.0})
                d["unc_kwh"] += kwh
            else:
                price = self._prices.get(start, default_price)
                self._corrections.append(SlotCorrection(
                    start=start, uncontrolled_ev_kwh=kwh,
                    uncontrolled_cost=kwh * price, real_cost=kwh * real_cost_per_kwh,
                    component=device_id,
                ))

    def _simulate(
        self,
        sample: LiveSample,
        household: float,
        forced: Mapping[str, float],
        hours: float,
        extra_load: float = 0.0,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """Flows of the uncontrolled house for one interval (AC kWh).

        *extra_load*: energy of the uncontrolled threshold devices — ordinary
        load, covered by PV before the batteries charge.
        """
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
        surplus = max(0.0, -(household + extra_load))
        if sample.pv_kw is not None:
            surplus = min(surplus, max(0.0, sample.pv_kw) * hours)
        # A battery regulating on the grid connection point on its own: the
        # surplus a separately billed load (heat pump) takes is gone for it.
        secondary_kwh = sum(max(0.0, kw) * hours for kw, _ in sample.secondary)
        site_surplus = max(0.0, surplus - secondary_kwh)
        for device_id, sc in self._batteries.items():
            if device_id not in led or surplus <= 1e-12:
                continue
            avail = min(surplus, site_surplus) if sc.own_meter == "grid_connection" else surplus
            take = min(avail, charge_headroom(device_id), room(device_id))
            if take > 0:
                charge[device_id] = charge.get(device_id, 0.0) + take
                surplus -= take
                site_surplus = max(0.0, site_surplus - take)

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
        deficit = household + extra_load + sum(charge.values()) + sum(ev_kwh.values())
        for device_id, sc in self._batteries.items():
            if device_id not in led or deficit <= 1e-12 or charge.get(device_id, 0.0) > 0:
                continue
            usable = max(0.0, led[device_id].stored_kwh - sc.capacity_kwh * sc.min_soc_pct / 100.0)
            give = min(deficit, sc.max_discharge_kw * hours, usable * sc.discharge_efficiency)
            if give > 0:
                discharge[device_id] = give
                deficit -= give
        return charge, discharge, ev_kwh

    def _step_devices(
        self, sample: LiveSample, previous: datetime, hours: float
    ) -> dict[str, float]:
        """Advance each uncontrolled thermostat by one interval; return the
        energy each device used (kWh)."""
        used: dict[str, float] = {}
        for device_id, tc in self._devices.items():
            sim = self.state.devices.setdefault(device_id, DeviceSim())
            live = sample.devices.get(device_id)
            if sim.value is None:
                if live is None or live.value is None:
                    continue
                self._resync_device(device_id, sim, live, sample.timestamp)
            model, ambient = self._thermal[device_id]
            mode = device_mode(tc)
            lo, hi = tc.bottom_threshold, tc.top_threshold
            if mode == "cool":
                want = sim.value >= hi or (sim.running and sim.value > lo)
            else:
                want = sim.value <= lo or (sim.running and sim.value < hi)
            if want != sim.running:
                since = datetime.fromisoformat(sim.since) if sim.since else None
                held_h = (previous - since).total_seconds() / 3600.0 if since else float("inf")
                if (sim.running and held_h < tc.min_runtime_h) or (
                    not sim.running and held_h < tc.min_offtime_h
                ):
                    want = sim.running
            if want != sim.running:
                sim.running = want
                sim.since = previous.isoformat()
            on = {mode: 1.0 if sim.running else 0.0}
            sim.value += hours * model.rate(sim.value, ambient, on)
            used[device_id] = tc.rated_power_kw * hours if sim.running else 0.0
        return used

    def _resync_device(
        self, device_id: str, sim: DeviceSim, live: DeviceLive, now: datetime
    ) -> None:
        """Set the simulated value to the measured one; unmatched energy is
        dropped (neutral).  The thermostat restarts as a thermostat would be
        at that value — off inside the band, whatever the planner is doing."""
        sim.value = live.value
        tc = self._devices.get(device_id)
        running = False
        if tc is not None and sim.value is not None:
            running = (
                sim.value >= tc.top_threshold if device_mode(tc) == "cool"
                else sim.value <= tc.bottom_threshold
            )
        if running != sim.running:
            sim.running, sim.since = running, now.isoformat()
        sim.real_kwh = sim.unc_kwh = 0.0
        sim.real_queue.clear()
        sim.unc_queue.clear()
        sim.last_resync = now.isoformat()

    def _resync_devices(self, sample: LiveSample, force: bool = False) -> None:
        """Resync every ``DEVICE_RESYNC_AFTER`` (or always with *force*)."""
        for device_id, sim in self.state.devices.items():
            live = sample.devices.get(device_id)
            if live is None or live.value is None:
                continue
            last = datetime.fromisoformat(sim.last_resync) if sim.last_resync else None
            if force or last is None or sample.timestamp - last >= DEVICE_RESYNC_AFTER:
                self._resync_device(device_id, sim, live, sample.timestamp)

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
        self._resync_devices(sample, force=True)

    def _roll(self, now: datetime) -> list[SavingsSlot]:
        """Close the open slot once *now* has left it."""
        slot = self._slot
        if slot is None or slot.start == slot_start(now, self._step_minutes):
            return []
        self._slot = None
        if slot.import_price is not None:
            self._prices[slot.start] = slot.import_price
            cutoff = now - timedelta(days=3)
            self._prices = {k: v for k, v in self._prices.items() if k >= cutoff}
        return [slot] if slot.hours > 0 else []


def reference_prices(
    slots: Sequence[tuple[datetime, float, float | None]],
    hours: float = REFERENCE_PRICE_HOURS,
) -> list[float | None]:
    """Time-weighted mean import price of the *hours* up to the end of each
    slot.  *slots*: ``(slot_start, hours, import_price)``, oldest first."""
    out: list[float | None] = []
    lo = 0
    sum_ph = sum_h = 0.0
    for start, h, price in slots:
        if price is not None and h > 0:
            sum_ph += price * h
            sum_h += h
        while slots[lo][0] <= start - timedelta(hours=hours):
            _, h0, p0 = slots[lo]
            if p0 is not None and h0 > 0:
                sum_ph -= p0 * h0
                sum_h -= h0
            lo += 1
        out.append(sum_ph / sum_h if sum_h > 1e-9 else None)
    return out


def stored_position(
    stored_detail: Mapping[str, Mapping[str, float]] | None,
    batteries: Mapping[str, StorageConstraints],
    price: float | None,
) -> dict[str, float] | None:
    """Saving still in each house's batteries, valued at *price*.

    *stored_detail*: a slot's ``{"real": {id: kWh}, "uncontrolled": …,
    "real_value": {id: €}, "uncontrolled_value": …}``.  Energy above the
    minimum SoC is what can still be used; used now, it would replace
    ``kWh × discharge efficiency`` of grid energy at *price* and cost its
    ledger share.  Returns ``real`` / ``uncontrolled`` (€) and their
    ``difference`` (real − uncontrolled: saving the real house has still to
    book), or None for slots recorded without ledger values.
    """
    if not stored_detail or price is None:
        return None
    result: dict[str, float] = {}
    for house in ("real", "uncontrolled"):
        kwh_by = stored_detail.get(house)
        value_by = stored_detail.get(f"{house}_value")
        if kwh_by is None or value_by is None:
            return None
        total = 0.0
        for device_id, kwh in kwh_by.items():
            sc = batteries.get(device_id)
            if sc is None or device_id not in value_by or kwh <= 1e-9:
                continue
            usable = max(0.0, kwh - sc.capacity_kwh * sc.min_soc_pct / 100.0)
            avg_cost = value_by[device_id] / kwh
            total += usable * (sc.discharge_efficiency * price - avg_cost)
        result[house] = total
    result["difference"] = result["real"] - result["uncontrolled"]
    return result


def fill_stored_values(
    details: Sequence[Mapping[str, Mapping[str, float]] | None],
) -> list[Mapping[str, Mapping[str, float]] | None]:
    """Give slots recorded before the ledger values were stored an estimate
    of them, so their stored energy can be valued too.

    *details*: the slots' ``stored_detail``, oldest first.  A slot without
    ``real_value`` / ``uncontrolled_value`` gets ``kWh × cost per kWh`` of the
    same house and battery from the latest earlier slot that has it, else from
    the first later one.  Without any such slot it stays as it is."""
    houses = ("real", "uncontrolled")
    # Cost per kWh per (house, device) of the first slot that has values.
    first_known: dict[tuple[str, str], float] = {}
    for detail in details:
        for house in houses:
            kwh_by = (detail or {}).get(house) or {}
            for device_id, value in ((detail or {}).get(f"{house}_value") or {}).items():
                kwh = kwh_by.get(device_id, 0.0)
                if kwh > 1e-9:
                    first_known.setdefault((house, device_id), value / kwh)
    if not first_known:
        return list(details)
    known = dict(first_known)
    out: list[Mapping[str, Mapping[str, float]] | None] = []
    for detail in details:
        if not detail:
            out.append(detail)
            continue
        filled = dict(detail)
        for house in houses:
            kwh_by = detail.get(house)
            if kwh_by is None:
                continue
            value_by = detail.get(f"{house}_value")
            if value_by is not None:
                for device_id, value in value_by.items():
                    kwh = kwh_by.get(device_id, 0.0)
                    if kwh > 1e-9:
                        known[(house, device_id)] = value / kwh
                continue
            filled[f"{house}_value"] = {
                device_id: kwh * known[(house, device_id)]
                for device_id, kwh in kwh_by.items()
                if (house, device_id) in known
            }
        out.append(filled)
    return out


def day_ahead_window_end(now: datetime, lead: timedelta) -> datetime:
    """End (exclusive) of the local day a plan made at *now* plans ahead for.

    Within *lead* before local midnight this is the end of the coming day;
    otherwise the end of the current day.
    """
    day = (now + lead).astimezone().date() + timedelta(days=1)
    # A naive datetime is read as local time, which keeps midnight correct
    # across daylight-saving changes.
    return datetime(day.year, day.month, day.day).astimezone(timezone.utc)

"""Vehicles at chargers: charging sessions and SoC estimation.

Sessions
--------
A session starts when a charger reports ``plugged`` and ends when it reports
unplugged.  Which vehicle it belongs to:

1. the vehicle (or guest) pre-selected for the charger's next plug-in, if any
   (``set_next``; used up by that plug-in);
2. else the charger's ``default_vehicle`` (if that vehicle isn't in a
   session at another charger already);
3. otherwise a **guest** (``vehicle_id is None``) — the charger then follows
   its ``guest_mode``;
4. reassigned by hand (UI / API / MQTT) to another known vehicle or to guest,
   valid until unplug.  A vehicle is in at most one session: assigning it to
   charger B turns its session at charger A into a guest session.

The charger's plug state is authoritative; the car's own plug sensor is only
shown.  There is no automatic matching.

SoC estimation
--------------
Car integrations report every 15–60 min, and not at all while the car sleeps.
Between two reports the SoC is dead-reckoned from the charger's energy::

    soc = last_reported_soc + Σ wall_kWh × efficiency(soc) / capacity

using the vehicle's charge curve.  Every fresh report resets the estimate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

#: ``assignment_source`` of a session.
DEFAULT = "default"
MANUAL = "manual"
PRESELECTED = "preselected"

#: ``next_vehicle`` value for a guest (no configured vehicle).
GUEST = "guest"

#: How a guest car is charged: PV surplus only, or full power now.
GUEST_PV = "pv_only"
GUEST_INSTANT = "instant"


# ---------------------------------------------------------------------------
# SoC estimation
# ---------------------------------------------------------------------------


def efficiency_at(curve: Iterable[tuple[float, float]], soc_pct: float) -> float:
    """Charging efficiency (battery kWh / wall kWh) at *soc_pct*.

    *curve* is ``[(soc_pct, efficiency), …]`` sorted by SoC; each point is
    the efficiency of the segment ending at that SoC.  Without a curve: 1.0.
    """
    last = 1.0
    for point_soc, eff in curve:
        last = eff
        if soc_pct < point_soc:
            return eff
    return last


@dataclass
class SocEstimate:
    """A vehicle's SoC: the car's last report plus charger energy since then."""

    soc_pct: float | None = None
    reported_soc_pct: float | None = None
    reported_at: datetime | None = None
    """When the car integration reported ``reported_soc_pct``."""
    added_kwh: float = 0.0
    """Wall energy charged since that report."""

    @property
    def estimated(self) -> bool:
        """True when charger energy was added on top of the car's report."""
        return self.added_kwh > 0.0


class SocEstimator:
    """Dead-reckons one vehicle's SoC between the car's own reports."""

    def __init__(self, capacity_kwh: float, curve: Iterable[tuple[float, float]] = ()) -> None:
        self._capacity_kwh = capacity_kwh
        self._curve = sorted(curve)
        self.state = SocEstimate()

    def observe(self, soc_pct: float | None, reported_at: datetime | None) -> bool:
        """Feed the car's latest reading; returns True when it reset the estimate.

        A reading counts as fresh when its report time is newer than the one
        the estimate is based on (or the value differs, for integrations
        without a report time).
        """
        if soc_pct is None:
            return False
        s = self.state
        fresh = (
            s.reported_soc_pct is None
            or (reported_at is not None and (s.reported_at is None or reported_at > s.reported_at))
            or (reported_at is None and soc_pct != s.reported_soc_pct)
        )
        if fresh:
            self.state = SocEstimate(
                soc_pct=soc_pct, reported_soc_pct=soc_pct, reported_at=reported_at,
            )
        return fresh

    def add_energy(self, wall_kwh: float) -> None:
        """Add *wall_kwh* drawn by the charger for this vehicle."""
        s = self.state
        if wall_kwh <= 0 or s.soc_pct is None or self._capacity_kwh <= 0:
            return
        gained = wall_kwh * efficiency_at(self._curve, s.soc_pct) / self._capacity_kwh * 100.0
        self.state = replace(
            s, soc_pct=min(100.0, s.soc_pct + gained), added_kwh=s.added_kwh + wall_kwh,
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass
class ChargingSession:
    """One plug-in at one charger."""

    charger_id: str
    vehicle_id: str | None
    """The vehicle being charged; ``None`` for a guest."""
    started_at: datetime
    source: str = DEFAULT
    """``default`` (the charger's default vehicle), ``preselected`` (chosen
    before plug-in) or ``manual`` (reassigned by hand while plugged in)."""
    session_id: int | None = None
    """Row id in storage, set by the caller once persisted."""

    @property
    def is_guest(self) -> bool:
        return self.vehicle_id is None


@dataclass(frozen=True)
class SessionChange:
    """A vehicle joined or left a charger."""

    charger_id: str
    vehicle_id: str | None
    started: bool
    """True: the vehicle (or a guest) is now at the charger; False: it left."""
    unplugged: bool = False
    """True when it left because the charger was unplugged (not reassigned)."""


@dataclass
class SessionManager:
    """Tracks which vehicle is at which charger."""

    default_vehicles: Mapping[str, str | None]
    """charger id → default vehicle id (or None)."""
    vehicles: frozenset[str] = frozenset()
    sessions: dict[str, ChargingSession] = field(default_factory=dict)
    """Open sessions by charger id."""
    next_vehicle: dict[str, str] = field(default_factory=dict)
    """charger id → vehicle id (or ``GUEST``) for its next plug-in."""
    guest_modes: dict[str, str] = field(default_factory=dict)
    """charger id → mode chosen for the guest there (``GUEST_PV`` /
    ``GUEST_INSTANT``); dropped when that guest leaves, so the next guest
    starts from the charger's configured ``guest_mode``."""

    def charger_of(self, vehicle_id: str) -> str | None:
        """The charger *vehicle_id* is at, or None."""
        for s in self.sessions.values():
            if s.vehicle_id == vehicle_id:
                return s.charger_id
        return None

    def restore(self, sessions: Iterable[ChargingSession]) -> None:
        """Open sessions from storage (before the first ``update``)."""
        for s in sessions:
            if s.charger_id not in self.default_vehicles:
                continue
            if s.vehicle_id is not None and (
                s.vehicle_id not in self.vehicles or self.charger_of(s.vehicle_id) is not None
            ):
                s = replace(s, vehicle_id=None)
            self.sessions[s.charger_id] = s

    def update(self, plugged: Mapping[str, bool | None], now: datetime) -> list[SessionChange]:
        """Apply the chargers' plug states; returns what changed.

        A charger with unknown plug state (None, or missing) keeps its session.
        """
        changes: list[SessionChange] = []
        for charger_id, is_plugged in plugged.items():
            if charger_id not in self.default_vehicles or is_plugged is None:
                continue
            session = self.sessions.get(charger_id)
            if not is_plugged and session is not None:
                del self.sessions[charger_id]
                changes.append(SessionChange(charger_id, session.vehicle_id, False, unplugged=True))
            elif is_plugged and session is None:
                changes += self._plug_in(charger_id, now)
        self.prune_guest_modes()
        return changes

    def guest_at(self, charger_id: str) -> bool:
        """A guest is plugged in at *charger_id*, or expected there next."""
        session = self.sessions.get(charger_id)
        if session is not None:
            return session.is_guest
        return self.next_vehicle.get(charger_id) == GUEST

    def prune_guest_modes(self) -> None:
        for cid in [c for c in self.guest_modes if not self.guest_at(c)]:
            del self.guest_modes[cid]

    def set_guest_mode(self, charger_id: str, mode: str) -> None:
        """Choose how the guest at (or expected at) *charger_id* charges."""
        if mode not in (GUEST_PV, GUEST_INSTANT):
            raise ValueError(f"guest mode must be {GUEST_PV} or {GUEST_INSTANT}, not {mode!r}")
        if charger_id not in self.default_vehicles:
            raise ValueError(f"unknown charger {charger_id!r}")
        if not self.guest_at(charger_id):
            raise ValueError(f"no guest car at {charger_id!r} — put the guest on it first")
        self.guest_modes[charger_id] = mode

    def _plug_in(self, charger_id: str, now: datetime) -> list[SessionChange]:
        changes: list[SessionChange] = []
        chosen = self.next_vehicle.pop(charger_id, None)
        if chosen is not None:
            vehicle = None if chosen == GUEST else chosen
            if vehicle is not None and (other := self.charger_of(vehicle)) is not None:
                # Pre-selected but still charging elsewhere: it is here now.
                self.sessions[other] = replace(self.sessions[other], vehicle_id=None, source=MANUAL)
                changes += [SessionChange(other, vehicle, False), SessionChange(other, None, True)]
            source = PRESELECTED
        else:
            default = self.default_vehicles.get(charger_id)
            vehicle = default if default and self.charger_of(default) is None else None
            source = DEFAULT
        self.sessions[charger_id] = ChargingSession(charger_id, vehicle, now, source)
        changes.append(SessionChange(charger_id, vehicle, True))
        return changes

    def set_next(self, charger_id: str, choice: str | None) -> None:
        """Pre-select the car for *charger_id*'s next plug-in: a vehicle id,
        ``GUEST``, or ``None`` for the charger's default.

        Raises ``ValueError`` for an unknown charger or vehicle, or when a car
        is plugged in there already (reassign it with ``assign`` instead).
        """
        if charger_id not in self.default_vehicles:
            raise ValueError(f"unknown charger {charger_id!r}")
        if choice is not None and choice != GUEST and choice not in self.vehicles:
            raise ValueError(f"unknown vehicle {choice!r}")
        if charger_id in self.sessions:
            raise ValueError(f"a car is plugged in at {charger_id!r} — reassign it instead")
        if choice is None:
            self.next_vehicle.pop(charger_id, None)
            return
        if choice != GUEST:  # a vehicle can only be expected at one charger
            for cid, other in list(self.next_vehicle.items()):
                if other == choice:
                    del self.next_vehicle[cid]
        self.next_vehicle[charger_id] = choice

    def assign(self, charger_id: str, vehicle_id: str | None, now: datetime) -> list[SessionChange]:
        """Reassign the car at *charger_id* by hand (``None`` = guest).

        Raises ``ValueError`` when nothing is plugged in there or the vehicle
        is unknown.
        """
        session = self.sessions.get(charger_id)
        if session is None:
            raise ValueError(f"no car is plugged in at {charger_id!r}")
        if vehicle_id is not None and vehicle_id not in self.vehicles:
            raise ValueError(f"unknown vehicle {vehicle_id!r}")
        if session.vehicle_id == vehicle_id:
            return []
        changes: list[SessionChange] = []
        if vehicle_id is not None and (other := self.charger_of(vehicle_id)) is not None:
            # The vehicle can only be in one place: its old charger falls back to guest.
            self.sessions[other] = replace(self.sessions[other], vehicle_id=None, source=MANUAL)
            changes += [SessionChange(other, vehicle_id, False), SessionChange(other, None, True)]
        changes.append(SessionChange(charger_id, session.vehicle_id, False))
        self.sessions[charger_id] = replace(session, vehicle_id=vehicle_id, source=MANUAL)
        changes.append(SessionChange(charger_id, vehicle_id, True))
        self.prune_guest_modes()
        return changes

    # ── per car: which charger ────────────────────────────────────────────

    def default_charger_of(self, vehicle_id: str) -> str | None:
        """The charger whose default vehicle *vehicle_id* is."""
        return next((c for c, v in self.default_vehicles.items() if v == vehicle_id), None)

    def select(self, vehicle_id: str | None, charger_id: str | None, now: datetime) -> list[SessionChange]:
        """Put a car (``None`` = a guest) on *charger_id*; the last selection wins.

        A car is plugged in there → it is reassigned now (``assign``).  The
        charger is free → the car is expected at its next plug-in, which uses
        the selection up, so after that car leaves the default applies again.
        Selecting a car's own default charger drops its selections elsewhere
        and any other car's selection there.  ``charger_id=None`` drops the
        car's selections (for a guest: every guest selection).
        """
        if vehicle_id is not None and vehicle_id not in self.vehicles:
            raise ValueError(f"unknown vehicle {vehicle_id!r}")
        choice = GUEST if vehicle_id is None else vehicle_id
        if charger_id is None:
            for cid, other in list(self.next_vehicle.items()):
                if other == choice:
                    del self.next_vehicle[cid]
            self.prune_guest_modes()
            return []
        if charger_id not in self.default_vehicles:
            raise ValueError(f"unknown charger {charger_id!r}")
        if charger_id in self.sessions:
            return self.assign(charger_id, vehicle_id, now)
        if vehicle_id is not None and self.default_vehicles.get(charger_id) == vehicle_id:
            self.select(vehicle_id, None, now)
            self.next_vehicle.pop(charger_id, None)
        else:
            self.set_next(charger_id, choice)
        self.prune_guest_modes()
        return []

    def status(self, vehicle_id: str) -> dict:
        """Where *vehicle_id* charges, as the UI shows it on the car.

        ``state`` is ``plugged`` (a session there), ``selected`` (expected at
        the next plug-in), ``default`` (its default charger, free for it),
        ``taken`` (its default charger is in use by, or selected for, another
        car or a guest: ``by``) or ``none``.
        """
        if (cid := self.charger_of(vehicle_id)) is not None:
            return {"charger_id": cid, "state": "plugged", "source": self.sessions[cid].source}
        chosen = next((c for c, v in self.next_vehicle.items() if v == vehicle_id), None)
        if chosen is not None:
            return {"charger_id": chosen, "state": "selected"}
        default = self.default_charger_of(vehicle_id)
        if default is None:
            return {"charger_id": None, "state": "none"}
        session = self.sessions.get(default)
        if session is not None:
            return {"charger_id": default, "state": "taken",
                    "by": session.vehicle_id or GUEST, "plugged": True}
        if (other := self.next_vehicle.get(default)) is not None:
            return {"charger_id": default, "state": "taken", "by": other, "plugged": False}
        return {"charger_id": default, "state": "default"}

    def guest_status(self) -> list[dict]:
        """The chargers a guest is plugged in at or expected at."""
        out = [{"charger_id": c, "state": "plugged"} for c, s in self.sessions.items() if s.is_guest]
        out += [{"charger_id": c, "state": "selected"} for c, v in self.next_vehicle.items() if v == GUEST]
        return out

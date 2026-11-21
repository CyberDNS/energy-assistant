"""VehicleFleet — vehicles, chargers and the sessions between them, at runtime.

Composition step for EVs (see ``core/composition.py`` for the rest):

=====================================  ======================================
devices + sessions                     composed into
=====================================  ======================================
vehicle + charger (session)            ``EvChargingAsset`` with
                                       ``asset_id`` = vehicle,
                                       ``device_id`` = charger, the charger's
                                       power limits → goals and the charger's
                                       ``EvChargerContributor``
vehicle without session                asset with ``device_id == ""``: no
                                       goal, still shown (SoC, next target)
charger with a guest car               the contributor follows ``guest_mode``
=====================================  ======================================

``compose`` runs on every poll: it updates the sessions from the chargers'
plug states, feeds the SoC estimators, and lays each vehicle's (estimated)
SoC over its charger's state, so goals, control and the UI read the car's SoC
at the charger as before.  The vehicle's own state gets the estimate too.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from ..core.device_model import DeviceCategory, DeviceSpec
from ..core.models import DeviceState
from ..core.vehicles import ChargingSession, SessionChange, SessionManager, SocEstimator
from .ev import EvChargerContributor, EvChargingAsset
from .loader import at_charger, guest_asset, vehicle_assets

# Longest poll gap integrated as charging energy (longer gaps: the energy is
# unknown, the next car report corrects the estimate).
_MAX_STEP = timedelta(seconds=60)


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


class VehicleFleet:
    """Vehicles, chargers and their sessions (see module docstring)."""

    def __init__(self, devices: Mapping[str, DeviceSpec]) -> None:
        self.vehicles: dict[str, DeviceSpec] = {
            s.id: s for s in devices.values() if s.category is DeviceCategory.VEHICLE
        }
        self.chargers: dict[str, DeviceSpec] = {
            s.id: s for s in devices.values() if s.category is DeviceCategory.CHARGER
        }
        self._templates = vehicle_assets(devices)
        self.sessions = SessionManager(
            default_vehicles={cid: s.params.get("default_vehicle") for cid, s in self.chargers.items()},
            vehicles=frozenset(self.vehicles),
        )
        self.estimators: dict[str, SocEstimator] = {
            vid: SocEstimator(
                asset.capacity_kwh,
                [(p.soc_pct, p.efficiency) for p in asset.charge_curve],
            )
            for vid, asset in self._templates.items()
        }
        self.contributors: dict[str, EvChargerContributor] = {
            cid: EvChargerContributor(guest_asset(spec)) for cid, spec in self.chargers.items()
        }
        self._last_poll: dict[str, datetime] = {}

    # ── composition ────────────────────────────────────────────────────

    def assets(self) -> list[EvChargingAsset]:
        """One asset per vehicle; ``device_id`` is its charger, or "" when away."""
        out = []
        for vid, template in self._templates.items():
            cid = self.sessions.charger_of(vid)
            if cid is None:
                out.append(template)
            else:
                out.append(at_charger(
                    template, self.chargers[cid], self.vehicles[vid].params.get("max_charge_kw"),
                ))
        return out

    def apply(self, disabled: set[str], force_charge: Mapping[str, float]) -> None:
        """Point each charger's contributor at the vehicle (or guest) now there."""
        assets = {a.asset_id: a for a in self.assets()}
        for cid, contrib in self.contributors.items():
            session = self.sessions.sessions.get(cid)
            vid = session.vehicle_id if session is not None else None
            if vid is not None:
                contrib.set_asset(assets[vid])
                contrib.set_guest_mode(None)
                contrib.set_disabled(vid in disabled)
                contrib.set_force_charge(force_charge.get(vid))
            else:
                contrib.set_asset(guest_asset(self.chargers[cid]))
                contrib.set_guest_mode(self.guest_mode(cid) if session is not None else None)
                contrib.set_disabled(False)
                contrib.set_force_charge(None)

    def compose(self, states: dict[str, DeviceState], now: datetime) -> list[SessionChange]:
        """Update sessions and SoC estimates from one poll; overlay *states* in place."""
        plugged = {}
        for cid in self.chargers:
            st = states.get(cid)
            value = st.extra.get("plugged") if st is not None else None
            plugged[cid] = value if isinstance(value, bool) else None
        changes = self.sessions.update(plugged, now)

        for vid, est in self.estimators.items():
            vs = states.get(vid)
            if vs is not None and vs.available:
                est.observe(vs.soc_pct, _ts(vs.extra.get("soc_timestamp")))

        for cid in self.chargers:
            last = self._last_poll.get(cid)
            self._last_poll[cid] = now
            session = self.sessions.sessions.get(cid)
            st = states.get(cid)
            if last is None or session is None or session.vehicle_id is None or st is None:
                continue
            if st.power_w is not None and st.power_w > 0:
                hours = min(now - last, _MAX_STEP).total_seconds() / 3600.0
                self.estimators[session.vehicle_id].add_energy(st.power_w / 1000.0 * hours)

        for cid in self.chargers:
            if (st := states.get(cid)) is not None:
                states[cid] = self._charger_state(cid, st, states)
        for vid in self.vehicles:
            if (vs := states.get(vid)) is not None:
                states[vid] = self._vehicle_state(vid, vs)
        return changes

    def _charger_state(
        self, cid: str, st: DeviceState, states: Mapping[str, DeviceState]
    ) -> DeviceState:
        session = self.sessions.sessions.get(cid)
        vid = session.vehicle_id if session is not None else None
        nxt = self.sessions.next_vehicle.get(cid)
        extra = {
            **st.extra, "vehicle_id": vid, "guest": session is not None and vid is None,
            "next_vehicle": nxt,
            # A guest is plugged in here, or expected here next.
            "guest_here": (session is not None and vid is None) or (session is None and nxt == "guest"),
            "guest_mode": self.guest_mode(cid),
        }
        soc = None
        if vid is not None:
            est = self.estimators[vid].state
            soc = est.soc_pct
            vs = states.get(vid)
            extra.update(
                soc_estimated=est.estimated,
                soc_reported_at=est.reported_at.isoformat() if est.reported_at else None,
                range_km=vs.extra.get("range_km") if vs is not None else None,
            )
        return st.model_copy(update={"soc_pct": soc, "extra": extra})

    def _vehicle_state(self, vid: str, vs: DeviceState) -> DeviceState:
        est = self.estimators[vid].state
        cid = self.sessions.charger_of(vid)
        session = self.sessions.sessions.get(cid) if cid else None
        extra = {
            **vs.extra,
            "charger_id": cid,
            "session_since": session.started_at.isoformat() if session else None,
            "soc_estimated": est.estimated,
            "soc_reported_pct": est.reported_soc_pct,
            "soc_reported_at": est.reported_at.isoformat() if est.reported_at else None,
            "charger_status": self.sessions.status(vid),
        }
        soc = est.soc_pct if est.soc_pct is not None else vs.soc_pct
        return vs.model_copy(update={
            "soc_pct": soc, "available": vs.available or soc is not None, "extra": extra,
        })

    # ── helpers for the server ─────────────────────────────────────────

    def guest_mode(self, charger_id: str) -> str:
        """How a guest at *charger_id* charges: chosen for this guest, else
        the charger's configured ``guest_mode`` (default PV only)."""
        return self.sessions.guest_modes.get(charger_id) or str(
            self.chargers[charger_id].params.get("guest_mode", "pv_only"))

    def session(self, charger_id: str) -> ChargingSession | None:
        return self.sessions.sessions.get(charger_id)

    def charger_vehicle_id(self, vehicle_id: str | None, charger_id: str) -> Any:
        """*vehicle_id*'s id in *charger_id*'s system (``charger_ids``), 0 for a
        guest, or None when the vehicle has no id there."""
        charger = self.chargers[charger_id]
        if vehicle_id is None:
            return 0
        ids = self.vehicles[vehicle_id].params.get("charger_ids") or {}
        return ids.get(charger.type)

    def soc_push_target(self, vehicle_id: str) -> tuple[str, Any] | None:
        """``(charger_id, id in that charger system)`` to publish *vehicle_id*'s
        SoC to: its current charger, else any charger of a system it has an
        id in; None when it has none or ``push_soc_to_charger`` is off."""
        spec = self.vehicles[vehicle_id]
        if not spec.params.get("push_soc_to_charger"):
            return None
        ids = spec.params.get("charger_ids") or {}
        current = self.sessions.charger_of(vehicle_id)
        order = ([current] if current else []) + [c for c in self.chargers if c != current]
        for cid in order:
            vid = ids.get(self.chargers[cid].type)
            if vid is not None:
                return cid, vid
        return None

    def previous_ids(self) -> dict[str, str]:
        """Former asset id → vehicle id, from the vehicles' ``previous_ids``."""
        return {
            old: vid for vid, spec in self.vehicles.items()
            for old in spec.params.get("previous_ids") or []
        }

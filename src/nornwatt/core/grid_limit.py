"""Grid connection limit — keep site import below a configured maximum.

The planner already keeps *planned* import below the limit (15-minute
averages).  This module enforces it on every control tick, against live
conditions the plan did not foresee (an inflexible load starting, a heat
pump defrost, a car drawing more than forecast).

Generic by design: any control contributor that implements the two methods
of ``CurtailableContributor`` takes part; nothing here knows device types.

Each tick
---------
1. Project the site import the new setpoints would cause::

       projected = grid_power − Σ measured_i + Σ expected_i

   For each participant, ``expected_i`` follows from the change of its
   setpoint relative to the previous tick: a lower setpoint gives
   ``min(measured, draw(new))``, a higher one ``measured + (draw(new) −
   draw(previous))``.  This stays right for devices that draw less than
   commanded (a car accepting less than the wallbox offers).
2. Above the limit: reduce participants in curtail order until the
   projection is at the limit.  Batteries are never pushed into discharge,
   EVs drop to their minimum charge power or stop.
3. Hysteresis: once curtailed, setpoints are only released when the
   projection with the full setpoints is at least ``hysteresis`` below the
   limit; within the band the curtailed setpoints are held, so loads near
   the limit do not toggle every tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .control import LiveSituation

_log = logging.getLogger(__name__)

DEFAULT_HYSTERESIS_FRACTION = 0.05
"""Release band when ``limit_hysteresis_kw`` is not configured: 5 % of the limit."""


@dataclass(frozen=True)
class GridLimit:
    """Site import limit at the grid connection point."""

    max_import_w: float
    curtail_order: tuple[str, ...] = ()
    """Device ids curtailed first → last.  Unlisted participants follow."""
    hysteresis_w: float | None = None
    """Release band below the limit; None → ``DEFAULT_HYSTERESIS_FRACTION``."""

    @property
    def release_w(self) -> float:
        hyst = (
            self.hysteresis_w
            if self.hysteresis_w is not None
            else self.max_import_w * DEFAULT_HYSTERESIS_FRACTION
        )
        return self.max_import_w - hyst


@runtime_checkable
class CurtailableContributor(Protocol):
    """A contributor whose setpoint can be reduced to respect the grid limit."""

    @property
    def device_id(self) -> str: ...

    def expected_draw_w(self, setpoint_w: float, live: "LiveSituation") -> float | None:
        """Grid-side power (W, + = consumption) this setpoint asks for, or
        None when the setpoint does not fix it (e.g. a wallbox in PV mode)."""
        ...

    def curtail_setpoint_w(
        self, setpoint_w: float, reduce_by_w: float, live: "LiveSituation"
    ) -> float:
        """A setpoint that draws up to *reduce_by_w* less (never more)."""
        ...


@dataclass
class _Participant:
    contributor: CurtailableContributor
    setpoint_w: float
    measured_w: float
    previous_w: float | None

    def expected_w(self, setpoint_w: float, live: "LiveSituation") -> float:
        new = self.contributor.expected_draw_w(setpoint_w, live)
        if new is None:
            return self.measured_w
        prev = (
            self.contributor.expected_draw_w(self.previous_w, live)
            if self.previous_w is not None
            else None
        )
        if prev is None:
            return new
        if new <= prev:
            return min(self.measured_w, new)
        return self.measured_w + (new - prev)


class GridLimitGuard:
    """Applies a ``GridLimit`` to the setpoints of one control tick."""

    def __init__(self, limit: GridLimit) -> None:
        self.limit = limit
        self._curtailed: dict[str, float] = {}

    @property
    def curtailed(self) -> dict[str, float]:
        """Setpoints currently held below what the plan asked for."""
        return dict(self._curtailed)

    def apply(
        self,
        setpoints: dict[str, float | None],
        contributors: list[object],
        live: "LiveSituation",
        previous: dict[str, float | None],
    ) -> dict[str, float | None]:
        """Return *setpoints* with participants curtailed to the limit."""
        participants = self._participants(setpoints, contributors, live, previous)
        if not participants:
            self._curtailed.clear()
            return setpoints

        def projected(sp: dict[str, float]) -> float:
            return live.grid_power_w + sum(
                p.expected_w(sp[did], live) - p.measured_w for did, p in participants.items()
            )

        desired = {did: p.setpoint_w for did, p in participants.items()}
        projected_full = projected(desired)
        result = dict(setpoints)

        if projected_full <= self.limit.release_w:
            if self._curtailed:
                _log.info(
                    "GridLimit: released curtailment of %s (projected %.0f W)",
                    ", ".join(sorted(self._curtailed)), projected_full,
                )
            self._curtailed.clear()
            return result

        if projected_full <= self.limit.max_import_w:
            # Within the hysteresis band: hold what is curtailed.
            for did, held in self._curtailed.items():
                if did in participants:
                    p = participants[did]
                    if p.expected_w(held, live) < p.expected_w(p.setpoint_w, live):
                        result[did] = held
            self._curtailed = {
                did: sp for did, sp in self._curtailed.items() if did in participants
            }
            return result

        excess_w = projected_full - self.limit.max_import_w
        curtailed: dict[str, float] = {}
        for did in self._order(participants):
            if excess_w <= 0.5:
                break
            p = participants[did]
            new_sp = p.contributor.curtail_setpoint_w(p.setpoint_w, excess_w, live)
            saved_w = p.expected_w(p.setpoint_w, live) - p.expected_w(new_sp, live)
            if saved_w <= 0.0:
                continue
            result[did] = new_sp
            curtailed[did] = new_sp
            excess_w -= saved_w
        if curtailed != self._curtailed:
            _log.info(
                "GridLimit: projected import %.0f W > %.0f W — curtailed %s%s",
                projected_full, self.limit.max_import_w,
                ", ".join(f"{did}: {participants[did].setpoint_w:.0f} → {sp:.0f} W"
                          for did, sp in curtailed.items()) or "nothing",
                f" (still {excess_w:.0f} W over)" if excess_w > 0.5 else "",
            )
        self._curtailed = curtailed
        return result

    def _participants(
        self,
        setpoints: dict[str, float | None],
        contributors: list[object],
        live: "LiveSituation",
        previous: dict[str, float | None],
    ) -> dict[str, _Participant]:
        result: dict[str, _Participant] = {}
        for c in contributors:
            if not isinstance(c, CurtailableContributor):
                continue
            sp = setpoints.get(c.device_id)
            if sp is None:
                continue
            state = live.device_states.get(c.device_id)
            measured = state.power_w if state is not None and state.power_w is not None else 0.0
            result[c.device_id] = _Participant(
                contributor=c, setpoint_w=sp, measured_w=measured,
                previous_w=previous.get(c.device_id),
            )
        return result

    def _order(self, participants: dict[str, _Participant]) -> list[str]:
        listed = [did for did in self.limit.curtail_order if did in participants]
        rest = [did for did in participants if did not in listed]
        # Default: storage before EVs (registration order within each group).
        rest.sort(key=lambda did: 0 if not getattr(participants[did].contributor, "is_ev", False) else 1)
        return listed + rest

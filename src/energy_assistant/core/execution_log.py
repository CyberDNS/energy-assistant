"""Execution log — what the control loop did with the plan, per slot.

Only the latest plan is kept in memory, and the savings predictions store
just cost totals per slot, so afterwards it is impossible to tell whether a
battery followed its plan or the control layer deviated from it.  The
execution log answers that: one row per accounting slot and controlled
device with

* the intent that was active (dominant mode, time-averaged planned power),
* the setpoint actually sent (time average) — for a device in observe mode
  (``control: observe``) the setpoint it *would* have been sent, flagged
  ``observed``, and
* the power actually measured (time average).

``ExecutionLog`` is a pure accumulator: feed it one ``ExecutionSample`` per
control tick and it returns each slot once it is complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from .savings import slot_start

# Ticks further apart than this are not bridged (restart, stalled loop).
_MAX_GAP_S = 300.0


@dataclass(frozen=True)
class DeviceTick:
    """What one device was told and did during one control tick."""

    mode: str
    planned_w: float | None
    """Planned power of the active intent (W, + = charge); ``None`` = no intent."""
    setpoint_w: float | None
    """Setpoint sent (W); ``None`` = no command."""
    measured_w: float | None
    """Measured power (W); ``None`` = no reading."""
    observed: bool = False
    """Observe mode: *setpoint_w* was computed but not sent ("would send")."""


@dataclass
class ExecutionSlot:
    """Time-weighted summary of one device over one accounting slot."""

    start: datetime
    device_id: str
    seconds: float = 0.0
    mode_seconds: dict[str, float] = field(default_factory=dict)
    planned_ws: float = 0.0
    """∫ planned power dt (W·s), over the time an intent was active."""
    planned_s: float = 0.0
    setpoint_ws: float = 0.0
    setpoint_s: float = 0.0
    measured_ws: float = 0.0
    measured_s: float = 0.0
    plan_created_at: datetime | None = None
    """Creation time of the last plan that drove this slot."""
    observed_s: float = 0.0
    """Time the device was in observe mode (setpoints not sent)."""

    @property
    def observed(self) -> bool:
        """The device was observed (not controlled) for most of the slot."""
        return self.observed_s * 2 > self.seconds

    @property
    def mode(self) -> str:
        """The mode that was active for most of the slot."""
        if not self.mode_seconds:
            return "no_plan"
        return max(self.mode_seconds.items(), key=lambda kv: kv[1])[0]

    @property
    def planned_w(self) -> float | None:
        return self.planned_ws / self.planned_s if self.planned_s > 0 else None

    @property
    def setpoint_w(self) -> float | None:
        return self.setpoint_ws / self.setpoint_s if self.setpoint_s > 0 else None

    @property
    def measured_w(self) -> float | None:
        return self.measured_ws / self.measured_s if self.measured_s > 0 else None


class ExecutionLog:
    """Accumulates control ticks into per-slot ``ExecutionSlot`` rows."""

    def __init__(self, step_minutes: int) -> None:
        self._step_minutes = step_minutes
        self._open: dict[str, ExecutionSlot] = {}
        self._slot: datetime | None = None
        self._last_ts: datetime | None = None

    def record(
        self,
        ts: datetime,
        devices: Mapping[str, DeviceTick],
        plan_created_at: datetime | None,
    ) -> list[ExecutionSlot]:
        """Book the time since the previous tick; return finished slots.

        Like the savings tracker, an interval belongs to the slot it starts
        in and is described by the tick that ends it.
        """
        previous, self._last_ts = self._last_ts, ts
        finished: list[ExecutionSlot] = []
        if previous is None:
            return finished
        dt = (ts - previous).total_seconds()
        start = slot_start(previous, self._step_minutes)
        if self._slot is not None and start != self._slot:
            finished = self._close()
        if dt <= 0 or dt > _MAX_GAP_S:
            # Not bridged; the open slot is complete once this tick left it.
            if self._slot is not None and slot_start(ts, self._step_minutes) != self._slot:
                finished += self._close()
            return finished
        self._slot = start
        for device_id, tick in devices.items():
            row = self._open.setdefault(device_id, ExecutionSlot(start=start, device_id=device_id))
            row.seconds += dt
            row.mode_seconds[tick.mode] = row.mode_seconds.get(tick.mode, 0.0) + dt
            if tick.planned_w is not None:
                row.planned_ws += tick.planned_w * dt
                row.planned_s += dt
            if tick.setpoint_w is not None:
                row.setpoint_ws += tick.setpoint_w * dt
                row.setpoint_s += dt
            if tick.measured_w is not None:
                row.measured_ws += tick.measured_w * dt
                row.measured_s += dt
            if tick.observed:
                row.observed_s += dt
            if plan_created_at is not None:
                row.plan_created_at = plan_created_at
        # A slot ends with the tick that leaves it: close it right away when
        # this tick already lies in the next slot.
        if slot_start(ts, self._step_minutes) != start:
            finished += self._close()
        return finished

    def _close(self) -> list[ExecutionSlot]:
        rows = [r for r in self._open.values() if r.seconds > 0]
        self._open = {}
        self._slot = None
        return rows

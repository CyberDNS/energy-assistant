"""Fail-safe: hand every controlled device back to its own operation when
Nornwatt can no longer be trusted to control it.

A device that is commanded through persistent settings (a battery held at
0 W, a wallbox on "stop", a cooler switched off) keeps that state when
nobody commands it any more.  ``FailSafe`` decides when control is
*released* — every device's ``release()`` puts it back to its normal
behaviour — and when Nornwatt *takes over* again:

========================  ==================================================
trigger                   what happens
========================  ==================================================
start, reload, stop       released by the application (``_release_devices``)
plan stale                no plan, a plan older than ``plan_stale_after_s``
                          or one that no longer covers the current time →
                          released; control resumes with the next fresh plan
control loop stuck        no completed control tick for
                          ``control_stuck_after_s`` (checked by ``watch()``,
                          a task independent of the control loop) →
                          released; control resumes with the next tick
========================  ==================================================

While released for a stale plan the control tick sends no commands (it
still books the batteries' measured energy).  Every release and takeover is
logged and kept in ``events`` for the status API / UI.

``FailSafe`` holds no device knowledge: *release_all* is the application's
coroutine that releases every device.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

_log = logging.getLogger(__name__)

#: Reasons, also the ``reason`` values of the status API.
START = "start"
PLAN_STALE = "plan_stale"
CONTROL_STUCK = "control_stuck"

_REASON_TEXT = {
    START: "Nornwatt is starting",
    PLAN_STALE: "no current plan",
    CONTROL_STUCK: "the control loop is stuck",
}

_MAX_EVENTS = 50


@dataclass(frozen=True)
class FailSafeEvent:
    at: datetime
    kind: str
    """``released`` or ``taken_over``."""
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "kind": self.kind, "reason": self.reason,
                "detail": self.detail}


def default_plan_stale_after_s(plan_interval_s: float) -> float:
    """45 min, and at least three planning intervals: one failed planning run
    must not release the devices, two in a row do."""
    return max(45 * 60.0, 3 * plan_interval_s)


def default_control_stuck_after_s(control_interval_s: float) -> float:
    """10 min, and at least ten control intervals."""
    return max(10 * 60.0, 10 * control_interval_s)


class FailSafe:
    """Release/takeover state of the control loop (see module docstring)."""

    def __init__(
        self,
        release_all: Callable[[], Awaitable[None]],
        *,
        plan_stale_after_s: float,
        control_stuck_after_s: float,
        check_interval_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._release_all = release_all
        self.plan_stale_after_s = plan_stale_after_s
        self.control_stuck_after_s = control_stuck_after_s
        self._check_interval_s = check_interval_s
        self._clock = clock
        self._now = now
        # Devices are released when the control task starts.
        self.released = True
        self.reason: str | None = START
        self.detail = ""
        self.since: datetime | None = now()
        self.events: deque[FailSafeEvent] = deque(maxlen=_MAX_EVENTS)
        self._last_tick: float = clock()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def plan_problem(self, plan: Any, now: datetime) -> str | None:
        """Why *plan* must not be executed at *now* (None = it may)."""
        if plan is None:
            return "no plan yet"
        age = now - plan.created_at
        if age > timedelta(seconds=self.plan_stale_after_s):
            return f"the newest plan is {age.total_seconds() / 60:.0f} min old"
        if plan.intents:
            end = max(i.timestep for i in plan.intents) + timedelta(minutes=plan.step_minutes)
            if now >= end:
                return f"the plan ended at {end.isoformat(timespec='minutes')}"
        return None

    async def check_plan(self, plan: Any, now: datetime) -> bool:
        """Called by every control tick before commanding the devices.

        Returns whether the tick may send commands.  A plan that turns stale
        releases the devices (once); the first fresh plan takes over again.
        """
        problem = self.plan_problem(plan, now)
        if problem is None:
            if self.released and self.reason in (START, PLAN_STALE):
                self._taken_over()
            return True
        if plan is None and self.reason == START:
            # Released at start, still waiting for the first plan.
            return False
        if not self.released or self.reason != PLAN_STALE:
            await self.release(PLAN_STALE, problem)
        return False

    # ------------------------------------------------------------------
    # Control loop liveness
    # ------------------------------------------------------------------

    def tick_done(self) -> None:
        """A control tick completed (whether or not it could send commands)."""
        self._last_tick = self._clock()
        if self.released and self.reason == CONTROL_STUCK:
            self._taken_over()

    def stuck_for_s(self) -> float | None:
        """Seconds since the last completed tick, when that counts as stuck."""
        idle = self._clock() - self._last_tick
        return idle if idle > self.control_stuck_after_s else None

    async def check_stuck(self) -> None:
        idle = self.stuck_for_s()
        if idle is not None and not (self.released and self.reason == CONTROL_STUCK):
            await self.release(CONTROL_STUCK, f"no control tick for {idle / 60:.0f} min")

    async def watch(self) -> None:
        """Watchdog task, independent of the control loop.  Never returns."""
        self._last_tick = self._clock()
        while True:
            await asyncio.sleep(self._check_interval_s)
            try:
                await self.check_stuck()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the watchdog must keep running
                _log.exception("Fail-safe watchdog check failed")

    # ------------------------------------------------------------------
    # Release / takeover
    # ------------------------------------------------------------------

    async def release(self, reason: str, detail: str = "") -> None:
        async with self._lock:
            self.released, self.reason, self.detail = True, reason, detail
            self.since = self._now()
            self.events.append(FailSafeEvent(self.since, "released", reason, detail))
            _log.warning(
                "Fail-safe: releasing all devices to their own operation — %s%s",
                _REASON_TEXT.get(reason, reason), f" ({detail})" if detail else "",
            )
            await self._release_all()

    def _taken_over(self) -> None:
        reason = self.reason or ""
        self.released, self.reason, self.detail = False, None, ""
        self.since = self._now()
        if reason != START:
            self.events.append(FailSafeEvent(self.since, "taken_over", reason))
            _log.warning("Fail-safe: taking over control again (was released: %s)",
                         _REASON_TEXT.get(reason, reason))
        else:
            _log.info("Fail-safe: first plan available — controlling the devices")

    def to_dict(self) -> dict[str, Any]:
        """Status API form."""
        return {
            "released": self.released,
            "reason": self.reason,
            "reason_text": _REASON_TEXT.get(self.reason or "", self.reason),
            "detail": self.detail,
            "since": self.since.isoformat() if self.since else None,
            "plan_stale_after_s": self.plan_stale_after_s,
            "control_stuck_after_s": self.control_stuck_after_s,
            "events": [e.to_dict() for e in reversed(self.events)],
        }

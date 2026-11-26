"""Slot-aligned planning schedule (issue #63).

The planning loop used to sleep a fixed interval after each run, so the
optimizer's runtime accumulated and plans drifted up to 12 min into the slot
they were meant for. Runs now happen ``plan_lead_s`` before each boundary.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import nornwatt.server as server_mod
from nornwatt.core.plan_schedule import aligned_plan_period, next_plan_run
from nornwatt.server import Application

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
LEAD = timedelta(seconds=30)
Q = timedelta(minutes=15)
H = timedelta(hours=1)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ---------------------------------------------------------------------------
# aligned_plan_period
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("interval_s", "step_min", "expected_s"),
    [
        (900, 15, 900),     # every slot
        (1800, 15, 1800),   # every 2nd slot
        (3600, 60, 3600),   # hourly slots
        (900, 60, 900),     # shorter than a slot: still hits every boundary
        (1000, 15, 900),    # snapped to whole slots
        (1400, 15, 1800),
        (1000, 60, 900),    # largest divisor of 60 min ≤ 1000 s
        (10, 15, 60),       # floor of one minute
    ],
)
def test_aligned_plan_period(interval_s: float, step_min: int, expected_s: int) -> None:
    assert aligned_plan_period(interval_s, step_min) == timedelta(seconds=expected_s)


# ---------------------------------------------------------------------------
# next_plan_run
# ---------------------------------------------------------------------------


def test_next_run_is_lead_before_next_boundary() -> None:
    assert next_plan_run(_utc(2026, 11, 17, 6, 51, 48), Q, LEAD) == _utc(2026, 11, 17, 6, 59, 30)


def test_next_run_skips_boundary_whose_lead_has_passed() -> None:
    # 06:59:40 is already past the 07:00 run time — next one is 07:14:30.
    assert next_plan_run(_utc(2026, 11, 17, 6, 59, 40), Q, LEAD) == _utc(2026, 11, 17, 7, 14, 30)


def test_next_run_is_strictly_after_now() -> None:
    at = _utc(2026, 11, 17, 6, 59, 30)
    assert next_plan_run(at, Q, LEAD) == at + Q


def test_after_guards_against_early_wakeup() -> None:
    scheduled = _utc(2026, 11, 17, 6, 59, 30)
    early = scheduled - timedelta(milliseconds=2)
    assert next_plan_run(early, Q, LEAD) == scheduled  # would repeat
    assert next_plan_run(early, Q, LEAD, after=scheduled) == scheduled + Q


def test_overrun_skips_missed_runs_without_catch_up() -> None:
    # A run starting at 06:59:30 that takes 40 min: the next run is the next
    # future one (07:44:30), not the three missed ones in a burst.
    scheduled = _utc(2026, 11, 17, 6, 59, 30)
    now = scheduled + timedelta(minutes=40)
    assert next_plan_run(now, Q, LEAD, after=scheduled) == _utc(2026, 11, 17, 7, 44, 30)


def test_hourly_slots() -> None:
    assert next_plan_run(_utc(2026, 11, 17, 6, 5), H, LEAD) == _utc(2026, 11, 17, 6, 59, 30)


def test_multiple_slots_period_lands_on_slot_boundary() -> None:
    nxt = next_plan_run(_utc(2026, 11, 17, 6, 1), 2 * Q, LEAD)
    assert nxt == _utc(2026, 11, 17, 6, 29, 30)


def test_accepts_non_utc_aware_now() -> None:
    now = datetime(2026, 11, 17, 7, 51, 48, tzinfo=BERLIN)  # = 06:51:48 UTC
    assert next_plan_run(now, Q, LEAD) == _utc(2026, 11, 17, 6, 59, 30)


def test_rejects_naive_now() -> None:
    with pytest.raises(ValueError):
        next_plan_run(datetime(2026, 11, 17, 6, 0), Q, LEAD)


@pytest.mark.parametrize(
    ("start", "period"),
    [
        # Spring forward: 2026-03-29 02:00 CET → 03:00 CEST (01:00 UTC).
        (datetime(2026, 3, 29, 1, 0, tzinfo=BERLIN), Q),
        (datetime(2026, 3, 29, 1, 0, tzinfo=BERLIN), H),
        # Fall back: 2026-10-25 03:00 CEST → 02:00 CET (01:00 UTC).
        (datetime(2026, 10, 25, 1, 0, tzinfo=BERLIN), Q),
        (datetime(2026, 10, 25, 1, 0, tzinfo=BERLIN), H),
    ],
)
def test_dst_change_keeps_runs_on_local_slot_boundaries(start: datetime, period: timedelta) -> None:
    now = start.astimezone(UTC) + timedelta(seconds=7)
    runs = []
    scheduled = None
    for _ in range(4 * 24 * 60 // int(period.total_seconds() // 60) // 4):  # ~6 h
        scheduled = next_plan_run(now, period, LEAD, after=scheduled)
        runs.append(scheduled)
        now = scheduled + timedelta(seconds=17)  # optimizer runtime
    for prev, cur in zip(runs, runs[1:]):
        assert cur - prev == period  # no drift, no gaps, no duplicates
    for run in runs:
        boundary = (run + LEAD).astimezone(BERLIN)
        assert boundary.second == 0
        assert boundary.minute % int(period.total_seconds() // 60) == 0


def test_no_drift_over_a_full_day() -> None:
    now = _utc(2026, 11, 17, 0, 0, 3)
    scheduled = None
    for _ in range(96):
        scheduled = next_plan_run(now, Q, LEAD, after=scheduled)
        boundary = scheduled + LEAD
        assert boundary.minute % 15 == 0 and boundary.second == 0
        now = scheduled + timedelta(seconds=20)
    assert scheduled == _utc(2026, 11, 17, 23, 59, 30)


# ---------------------------------------------------------------------------
# Application._planning_loop
# ---------------------------------------------------------------------------


class _Clock:
    """Fake wall clock driven by the patched asyncio.sleep."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def datetime_cls(self) -> type:
        clock = self

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return clock.now.astimezone(tz) if tz else clock.now

        return _FakeDatetime


async def _drive_loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start: datetime,
    runtime: list[timedelta],
    fail: set[int] = frozenset(),  # type: ignore[assignment]
    n_runs: int,
) -> tuple[list[datetime], list[datetime]]:
    """Run the planning loop for *n_runs* runs; return (run starts, run ends)."""
    clock = _Clock(start)
    monkeypatch.setattr(server_mod, "datetime", clock.datetime_cls())

    app = Application()
    app._plan_interval_s = 900.0
    app._plan_lead_s = 30.0
    app._optimizer = SimpleNamespace(_step_min=15)
    app._first_poll_done = asyncio.Event()
    app._first_poll_done.set()

    starts: list[datetime] = []
    ends: list[datetime] = []
    done = asyncio.Event()

    async def fake_run_plan() -> None:
        i = len(starts)
        starts.append(clock.now)
        clock.now += runtime[i % len(runtime)]
        ends.append(clock.now)
        if len(starts) >= n_runs:
            done.set()
            await asyncio.Event().wait()  # park until cancelled
        if i in fail:
            raise RuntimeError("solver blew up")

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        assert seconds >= 0
        clock.now += timedelta(seconds=seconds)
        await real_sleep(0)

    app._run_plan = fake_run_plan  # type: ignore[method-assign]
    monkeypatch.setattr(server_mod.asyncio, "sleep", fake_sleep)
    task = asyncio.create_task(app._planning_loop())
    await asyncio.wait_for(done.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return starts, ends


@pytest.mark.asyncio
async def test_planning_loop_runs_aligned_without_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    starts, ends = await _drive_loop(
        monkeypatch,
        start=_utc(2026, 11, 17, 6, 51, 48),
        runtime=[timedelta(seconds=18)],
        n_runs=100,
    )
    # First run is immediate (startup), then every run is 30 s before a slot.
    assert starts[0] == _utc(2026, 11, 17, 6, 51, 48)
    for s in starts[1:]:
        assert (s + LEAD).minute % 15 == 0 and (s + LEAD).second == 0
    assert starts[-1] == _utc(2026, 11, 17, 6, 59, 30) + 98 * Q
    # Each plan is published before its slot starts.
    for s, e in zip(starts[1:], ends[1:]):
        assert e < s + LEAD


@pytest.mark.asyncio
async def test_planning_loop_failure_and_overrun_do_not_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    starts, _ = await _drive_loop(
        monkeypatch,
        start=_utc(2026, 11, 17, 6, 59, 0),
        # Run #1 overruns by 20 min, run #2 fails instantly.
        runtime=[timedelta(seconds=5), timedelta(minutes=20), timedelta(0), timedelta(seconds=5)],
        fail={2},
        n_runs=5,
    )
    assert starts == [
        _utc(2026, 11, 17, 6, 59, 0),
        _utc(2026, 11, 17, 6, 59, 30),
        _utc(2026, 11, 17, 7, 29, 30),   # 07:14:30 missed during overrun — skipped
        _utc(2026, 11, 17, 7, 44, 30),   # failed run doesn't re-run immediately
        _utc(2026, 11, 17, 7, 59, 30),
    ]


@pytest.mark.asyncio
async def test_event_replan_does_not_shift_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replan triggered between scheduled runs leaves the grid alone."""
    clock = _Clock(_utc(2026, 11, 17, 6, 50, 0))
    monkeypatch.setattr(server_mod, "datetime", clock.datetime_cls())

    app = Application()
    app._plan_interval_s = 900.0
    app._plan_lead_s = 30.0
    app._optimizer = SimpleNamespace(_step_min=15)
    app._first_poll_done = asyncio.Event()
    app._first_poll_done.set()

    starts: list[tuple[str, datetime]] = []
    done = asyncio.Event()

    async def fake_run_plan(source: str = "loop") -> None:
        starts.append((source, clock.now))
        clock.now += timedelta(seconds=10)
        if sum(1 for s, _ in starts if s == "loop") >= 4:
            done.set()
            await asyncio.Event().wait()

    real_sleep = asyncio.sleep
    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            # Mid-way through the second wait, an EV connects.
            clock.now += timedelta(seconds=seconds / 2)
            await fake_run_plan("event")
            clock.now += timedelta(seconds=seconds / 2) - timedelta(seconds=10)
        else:
            clock.now += timedelta(seconds=seconds)
        await real_sleep(0)

    app._run_plan = fake_run_plan  # type: ignore[method-assign]
    monkeypatch.setattr(server_mod.asyncio, "sleep", fake_sleep)
    task = asyncio.create_task(app._planning_loop())
    await asyncio.wait_for(done.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    loop_starts = [t for s, t in starts if s == "loop"]
    assert loop_starts == [
        _utc(2026, 11, 17, 6, 50, 0),
        _utc(2026, 11, 17, 6, 59, 30),
        _utc(2026, 11, 17, 7, 14, 30),
        _utc(2026, 11, 17, 7, 29, 30),
    ]
    assert any(s == "event" for s, _ in starts)

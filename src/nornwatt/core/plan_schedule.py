"""Slot-aligned schedule for the periodic planning run.

The planning loop used to sleep a fixed ``plan_interval_s`` *after* each run,
so the optimizer's runtime accumulated and plans drifted later and later into
the slot they were meant for. Instead, each run is now scheduled on a fixed
grid: shortly (``plan_lead_s``) before a slot boundary, so the fresh plan is
published by the time the slot starts.

The grid is anchored at the Unix epoch in UTC, the same grid the optimizer
floors its timestamps to. A daylight-saving change shifts local time by a
whole hour, so it does not move the slot boundaries.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def aligned_plan_period(plan_interval_s: float, step_minutes: int) -> timedelta:
    """Snap ``plan_interval_s`` to a period that keeps runs on slot boundaries.

    - At least one slot: rounded to the nearest whole number of slots
      ("plan every N slots").
    - Shorter than one slot: the largest whole-minute divisor of the slot
      length that is not longer than the interval, so every slot boundary is
      still one of the run times (e.g. 900 s with 60-min slots → 15 min).
    """
    step_s = step_minutes * 60
    interval = max(60.0, float(plan_interval_s))
    if interval >= step_s:
        period_s = max(1, round(interval / step_s)) * step_s
    else:
        period_s = max(
            d * 60 for d in range(1, step_minutes + 1)
            if step_minutes % d == 0 and d * 60 <= interval
        )
    return timedelta(seconds=period_s)


def next_plan_run(
    now: datetime,
    period: timedelta,
    lead: timedelta,
    after: datetime | None = None,
) -> datetime:
    """Return the next scheduled run time strictly after *now* (and *after*).

    Runs happen at ``boundary - lead`` for every boundary on the epoch-anchored
    grid of *period*. Missed runs are skipped, never caught up: if a run
    overran past one or more scheduled times, the result is simply the next
    one still ahead. *after* (the previous scheduled time) guards against
    running twice for the same boundary when a sleep wakes up a hair early.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    ref = now if after is None else max(now, after)
    period_s = period.total_seconds()
    # Boundaries b with b - lead > ref  ⇔  b > ref + lead.
    k = math.floor((ref + lead - _EPOCH).total_seconds() / period_s) + 1
    return _EPOCH + timedelta(seconds=k * period_s) - lead

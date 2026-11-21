"""A car arriving at a charger triggers an immediate replan.

Without this, a car plugged in shortly after a planning cycle would sit
without a charging goal for up to plan_interval_s.  The trigger is the start
of a charging session (``Application._on_session_changes``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from helpers.fleet_app import fleet_app as _app

from energy_assistant.core.models import DeviceState

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _poll(app, plugged: bool, at: datetime, calls: list[int]) -> None:
    states = {
        "wallbox": DeviceState(device_id="wallbox", available=plugged, extra={"plugged": plugged}),
        "ev1": DeviceState(device_id="ev1", soc_pct=40.0, extra={"soc_timestamp": T0.isoformat()}),
    }
    changes = app._fleet.compose(states, at)
    if changes:
        await app._on_session_changes(changes, replan=True, now=at)
    await asyncio.sleep(0)


def _counting(app) -> list[int]:
    calls: list[int] = []

    async def fake_run_plan() -> None:
        calls.append(1)

    app._run_plan = fake_run_plan
    return calls


async def test_plug_in_triggers_replan_once() -> None:
    app = _app()
    calls = _counting(app)
    await _poll(app, False, T0, calls)
    assert calls == []
    await _poll(app, True, T0 + timedelta(minutes=1), calls)
    assert calls == [1]
    await _poll(app, True, T0 + timedelta(minutes=2), calls)   # staying plugged
    assert calls == [1]


async def test_unplug_replans_too() -> None:
    """Leaving frees the charger's plan slots for the rest of the house."""
    app = _app()
    calls = _counting(app)
    await _poll(app, True, T0, calls)
    await _poll(app, False, T0 + timedelta(minutes=1), calls)
    assert calls == [1, 1]

"""Tests for EventBus."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from energy_manager.core.event import DeviceStateEvent, EventBus, PlanUpdatedEvent
from energy_manager.core.models import DeviceState, EnergyPlan


def _state(device_id: str = "test") -> DeviceState:
    return DeviceState(device_id=device_id, timestamp=datetime.now(timezone.utc), power_w=100.0)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


async def test_publish_then_flush_dispatches_event(bus: EventBus) -> None:
    received: list[DeviceStateEvent] = []

    async def handler(event: DeviceStateEvent) -> None:
        received.append(event)

    bus.subscribe(DeviceStateEvent, handler)
    await bus.publish(DeviceStateEvent(state=_state()))

    # Not yet dispatched before flush.
    assert received == []

    await bus.flush()
    assert len(received) == 1
    assert received[0].state.device_id == "test"


async def test_multiple_subscribers_all_called(bus: EventBus) -> None:
    calls: list[str] = []

    async def h1(e: DeviceStateEvent) -> None:
        calls.append("h1")

    async def h2(e: DeviceStateEvent) -> None:
        calls.append("h2")

    bus.subscribe(DeviceStateEvent, h1)
    bus.subscribe(DeviceStateEvent, h2)

    await bus.publish(DeviceStateEvent(state=_state()))
    await bus.flush()

    assert calls == ["h1", "h2"]


async def test_handlers_called_in_fifo_order(bus: EventBus) -> None:
    order: list[int] = []

    for i in range(5):
        async def handler(e: DeviceStateEvent, _i: int = i) -> None:
            order.append(_i)
        bus.subscribe(DeviceStateEvent, handler)

    await bus.publish(DeviceStateEvent(state=_state()))
    await bus.flush()

    assert order == [0, 1, 2, 3, 4]


async def test_only_matching_type_handler_called(bus: EventBus) -> None:
    state_called: list[bool] = []
    plan_called: list[bool] = []

    async def on_state(e: DeviceStateEvent) -> None:
        state_called.append(True)

    async def on_plan(e: PlanUpdatedEvent) -> None:
        plan_called.append(True)

    bus.subscribe(DeviceStateEvent, on_state)
    bus.subscribe(PlanUpdatedEvent, on_plan)

    await bus.publish(DeviceStateEvent(state=_state()))
    await bus.flush()

    assert state_called == [True]
    assert plan_called == []


async def test_flush_clears_queue(bus: EventBus) -> None:
    called: list[int] = []

    async def handler(e: DeviceStateEvent) -> None:
        called.append(1)

    bus.subscribe(DeviceStateEvent, handler)
    await bus.publish(DeviceStateEvent(state=_state()))
    await bus.flush()
    await bus.flush()  # second flush must not replay

    assert len(called) == 1


async def test_multiple_events_dispatched_in_order(bus: EventBus) -> None:
    ids: list[str] = []

    async def handler(e: DeviceStateEvent) -> None:
        ids.append(e.state.device_id)

    bus.subscribe(DeviceStateEvent, handler)

    for device_id in ["a", "b", "c"]:
        await bus.publish(DeviceStateEvent(state=_state(device_id)))

    await bus.flush()
    assert ids == ["a", "b", "c"]


async def test_no_handlers_registered(bus: EventBus) -> None:
    # Publishing and flushing with no subscribers must not raise.
    await bus.publish(DeviceStateEvent(state=_state()))
    await bus.flush()


async def test_plan_updated_event(bus: EventBus) -> None:
    received: list[PlanUpdatedEvent] = []

    async def handler(e: PlanUpdatedEvent) -> None:
        received.append(e)

    bus.subscribe(PlanUpdatedEvent, handler)
    plan = EnergyPlan()
    await bus.publish(PlanUpdatedEvent(plan=plan))
    await bus.flush()

    assert len(received) == 1
    assert received[0].plan is plan

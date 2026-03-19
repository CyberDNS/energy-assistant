"""Tests for EventBus."""

from __future__ import annotations

import pytest

from energy_assistant.core.event import DeviceStateEvent, Event, EventBus, PlanUpdatedEvent
from energy_assistant.core.models import DeviceRole, DeviceState, EnergyPlan


def _make_state(device_id: str = "meter", power_w: float = 1000.0) -> DeviceState:
    return DeviceState(device_id=device_id, power_w=power_w)


class TestEventBus:
    async def test_publish_and_flush_delivers_event(self) -> None:
        bus = EventBus()
        received: list[DeviceStateEvent] = []

        async def handler(event: DeviceStateEvent) -> None:
            received.append(event)

        bus.subscribe(DeviceStateEvent, handler)
        await bus.publish(DeviceStateEvent(state=_make_state()))
        assert received == []  # not delivered yet

        await bus.flush()
        assert len(received) == 1
        assert received[0].state.device_id == "meter"

    async def test_multiple_handlers_all_called(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        async def h1(e: DeviceStateEvent) -> None:
            calls.append("h1")

        async def h2(e: DeviceStateEvent) -> None:
            calls.append("h2")

        bus.subscribe(DeviceStateEvent, h1)
        bus.subscribe(DeviceStateEvent, h2)
        await bus.publish(DeviceStateEvent(state=_make_state()))
        await bus.flush()

        assert calls == ["h1", "h2"]

    async def test_flush_drains_in_fifo_order(self) -> None:
        bus = EventBus()
        order: list[float] = []

        async def handler(event: DeviceStateEvent) -> None:
            order.append(event.state.power_w)

        bus.subscribe(DeviceStateEvent, handler)
        await bus.publish(DeviceStateEvent(state=_make_state(power_w=1.0)))
        await bus.publish(DeviceStateEvent(state=_make_state(power_w=2.0)))
        await bus.publish(DeviceStateEvent(state=_make_state(power_w=3.0)))
        await bus.flush()

        assert order == [1.0, 2.0, 3.0]

    async def test_handler_not_called_for_different_event_type(self) -> None:
        bus = EventBus()
        called = False

        async def handler(event: DeviceStateEvent) -> None:
            nonlocal called
            called = True

        bus.subscribe(DeviceStateEvent, handler)
        await bus.publish(PlanUpdatedEvent(plan=EnergyPlan()))
        await bus.flush()

        assert not called

    async def test_events_published_during_flush_deferred(self) -> None:
        """Events queued by a handler are delivered on the *next* flush, not the current one."""
        bus = EventBus()
        second_deliveries: list[str] = []

        async def handler(event: DeviceStateEvent) -> None:
            if event.state.device_id == "second":
                second_deliveries.append("got_second")
            else:
                # while processing "first", queue a "second" event
                await bus.publish(DeviceStateEvent(state=_make_state(device_id="second")))

        bus.subscribe(DeviceStateEvent, handler)

        await bus.publish(DeviceStateEvent(state=_make_state(device_id="first")))
        await bus.flush()
        assert second_deliveries == []  # "second" not yet delivered

        await bus.flush()
        assert second_deliveries == ["got_second"]

    async def test_flush_on_empty_bus_is_safe(self) -> None:
        bus = EventBus()
        await bus.flush()  # should not raise

    async def test_multiple_flushes_do_not_redeliver(self) -> None:
        bus = EventBus()
        calls = 0

        async def handler(event: DeviceStateEvent) -> None:
            nonlocal calls
            calls += 1

        bus.subscribe(DeviceStateEvent, handler)
        await bus.publish(DeviceStateEvent(state=_make_state()))
        await bus.flush()
        await bus.flush()

        assert calls == 1

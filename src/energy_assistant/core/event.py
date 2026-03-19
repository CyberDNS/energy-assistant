"""EventBus — the nervous system of the platform.

All communication between platform layers goes through the event bus.
No layer holds a direct reference to another layer's implementation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .models import DeviceState, EnergyPlan

T = TypeVar("T", bound="Event")


@dataclass
class Event:
    """Base class for all events published on the bus."""


@dataclass
class DeviceStateEvent(Event):
    """Published by the polling loop each time a device's state is read."""

    state: DeviceState


@dataclass
class PlanUpdatedEvent(Event):
    """Published by the planning loop when the optimizer produces a new plan."""

    plan: EnergyPlan


class EventBus:
    """FIFO async event dispatcher.

    Usage
    -----
    ::

        bus = EventBus()

        async def on_state(event: DeviceStateEvent) -> None:
            print(event.state.power_w)

        bus.subscribe(DeviceStateEvent, on_state)
        await bus.publish(DeviceStateEvent(state=...))
        await bus.flush()   # delivers events to handlers

    ``flush()`` delivers all events queued since the last flush.
    Events queued *during* flush are deferred to the next flush.
    This gives tests full deterministic control over ordering.
    """

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[..., Awaitable[None]]]] = defaultdict(list)
        self._pending: list[Event] = []

    def subscribe(
        self,
        event_type: type[T],
        handler: Callable[[T], Awaitable[None]],
    ) -> None:
        """Register a coroutine handler for *event_type*."""
        self._handlers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        """Queue *event* for delivery on the next ``flush()``."""
        self._pending.append(event)

    async def flush(self) -> None:
        """Drain all pending events, invoking every registered handler.

        Safe to call repeatedly.  Events produced by handlers are deferred
        to the next flush (no infinite recursion).
        """
        events, self._pending = self._pending, []
        for event in events:
            for handler in self._handlers.get(type(event), []):
                await handler(event)

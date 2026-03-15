"""
Event bus and domain event types.

All inter-module communication passes through the EventBus.  No layer holds
a direct reference to another layer's implementation.

Usage pattern
-------------
    bus = EventBus()

    async def on_state(event: DeviceStateEvent) -> None:
        print(event.state.power_w)

    bus.subscribe(DeviceStateEvent, on_state)

    await bus.publish(DeviceStateEvent(state=...))
    await bus.flush()   # dispatches all pending events in order

In production the main loop calls ``flush()`` after each polling cycle.
In tests ``flush()`` gives full control over dispatch ordering.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .models import DeviceState, EnergyPlan

T = TypeVar("T", bound="Event")


# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """Base class for all domain events."""


# ---------------------------------------------------------------------------
# Built-in event types
# ---------------------------------------------------------------------------


@dataclass
class DeviceStateEvent(Event):
    """Published by a device whenever its state is refreshed."""

    state: DeviceState


@dataclass
class PlanUpdatedEvent(Event):
    """Published by the optimizer whenever a new EnergyPlan is produced."""

    plan: EnergyPlan


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


class EventBus:
    """
    Lightweight in-process event bus.

    ``publish()`` queues an event without dispatching it immediately.
    ``flush()`` dispatches all queued events in FIFO order, invoking every
    registered handler for each event type.
    """

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[..., Awaitable[None]]]] = defaultdict(list)
        self._pending: list[Event] = []

    def subscribe(
        self,
        event_type: type[T],
        handler: Callable[[T], Awaitable[None]],
    ) -> None:
        """Register *handler* to be called for every event of *event_type*."""
        self._handlers[event_type].append(handler)  # type: ignore[arg-type]

    async def publish(self, event: Event) -> None:
        """Enqueue *event* for dispatch on the next ``flush()`` call."""
        self._pending.append(event)

    async def flush(self) -> None:
        """
        Dispatch all pending events in order and clear the queue.

        Each handler is awaited sequentially.  Handlers added during flush
        are not called until the *next* flush.
        """
        events, self._pending = self._pending, []
        for event in events:
            for handler in self._handlers.get(type(event), []):
                await handler(event)

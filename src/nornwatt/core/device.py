"""Device protocol — structural interface for any energy-system component."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .device_model import DeviceCategory
from .models import DeviceCommand, DeviceState


@runtime_checkable
class Device(Protocol):
    """Structural interface for any energy-system component.

    Plugins implement this protocol without inheriting from it — they only
    need to structurally match the interface (duck-typing via Protocol).
    """

    @property
    def device_id(self) -> str:
        """Stable, unique identifier for this device."""
        ...

    @property
    def category(self) -> DeviceCategory:
        """What this device *is* in the energy system (from ``category:``)."""
        ...

    async def get_state(self) -> DeviceState:
        """Read and return the device's current state snapshot."""
        ...

    async def send_command(self, command: DeviceCommand) -> None:
        """Send a control command to the device.

        Read-only devices may ignore commands silently.
        """
        ...

"""
Device protocol and category enum.

Plugins implement the ``Device`` protocol without subclassing — structural
typing (duck typing) is sufficient.  This keeps third-party plugins fully
decoupled from the core package.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import DeviceCategory, DeviceCommand, DeviceState


@runtime_checkable
class Device(Protocol):
    """
    Represents any physical or virtual component that produces, consumes,
    or stores energy, or measures energy flow.

    Implementors must not subclass this — they only need to match the
    interface structurally.
    """

    @property
    def device_id(self) -> str:
        """Stable unique identifier for this device (e.g. ``"solar_inverter"``)."""
        ...

    @property
    def category(self) -> DeviceCategory:
        """Broad energy role of this device."""
        ...

    async def get_state(self) -> DeviceState:
        """
        Return the current normalised state of the device.

        Implementations should not cache heavily; the caller decides polling
        frequency.
        """
        ...

    async def send_command(self, command: DeviceCommand) -> None:
        """
        Send a control command to the device.

        Raises ``NotImplementedError`` for read-only devices (e.g. meters).
        """
        ...

"""
Device registry — the central catalogue of all active devices.
"""

from __future__ import annotations

from .device import Device
from .models import DeviceCategory


class DeviceRegistry:
    """
    Maintains the set of active ``Device`` instances indexed by ``device_id``.

    The registry holds no domain logic; it is purely a lookup structure used
    by the optimizer, storage layer, and HTTP API.
    """

    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}

    def register(self, device: Device) -> None:
        """Add *device* to the registry, replacing any existing entry with the same id."""
        self._devices[device.device_id] = device

    def unregister(self, device_id: str) -> None:
        """Remove the device with *device_id*.  No-op if not present."""
        self._devices.pop(device_id, None)

    def get(self, device_id: str) -> Device | None:
        """Return the device for *device_id*, or ``None``."""
        return self._devices.get(device_id)

    def all(self) -> list[Device]:
        """Return all registered devices in insertion order."""
        return list(self._devices.values())

    def by_category(self, category: DeviceCategory) -> list[Device]:
        """Return all devices whose category matches *category*."""
        return [d for d in self._devices.values() if d.category == category]

"""DeviceRegistry — central catalogue of all registered devices.

The registry also caches the last-known state for each device so that
derived (differential) devices can access recent readings without
triggering additional API calls.
"""

from __future__ import annotations

from .device import Device
from .models import DeviceRole, DeviceState


class DeviceRegistry:
    """Catalogue of all known devices, keyed by ``device_id``.

    The polling loop writes state snapshots via ``update_state()``.
    Derived devices (e.g. ``DifferentialDevice``) can read back the
    cached state via ``latest_state()`` rather than making extra API calls.
    """

    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._latest_states: dict[str, DeviceState] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, device: Device) -> None:
        """Add *device* to the catalogue. Overwrites any existing entry."""
        self._devices[device.device_id] = device

    def unregister(self, device_id: str) -> None:
        """Remove the device with *device_id* from the catalogue."""
        self._devices.pop(device_id, None)
        self._latest_states.pop(device_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, device_id: str) -> Device | None:
        """Return the device with *device_id*, or None if not registered."""
        return self._devices.get(device_id)

    def all(self) -> list[Device]:
        """Return all registered devices."""
        return list(self._devices.values())

    def by_role(self, role: DeviceRole) -> list[Device]:
        """Return all devices with the given *role*."""
        return [d for d in self._devices.values() if d.role == role]

    def __len__(self) -> int:
        return len(self._devices)

    # ------------------------------------------------------------------
    # State cache
    # ------------------------------------------------------------------

    def update_state(self, state: DeviceState) -> None:
        """Store the most recent state snapshot for a device.

        Called by the polling loop after each ``device.get_state()`` call.
        """
        self._latest_states[state.device_id] = state

    def latest_state(self, device_id: str) -> DeviceState | None:
        """Return the last cached state, or None if never polled."""
        return self._latest_states.get(device_id)

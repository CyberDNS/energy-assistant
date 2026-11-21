"""TibberLocalDevice — meter power read from the Tibber Pulse Bridge on the LAN.

``power_w`` is the net active power (OBIS 16.7.0; positive = importing,
negative = exporting).  ``energy_kwh`` is the import counter (1.8.0).
``extra`` carries the export counter, per-phase power/voltage/current, grid
frequency, node diagnostics and ``source`` (``local`` or ``cloud``).

When a cloud ``fallback`` device is configured, it is used while the bridge
has no fresh reading.  Its live stream is kept running so it is warm when needed.
"""

from __future__ import annotations

from typing import Any

from ...core.device_model import DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .bridge import PulseBridgePoller


class TibberLocalDevice:
    """A read-only meter ``Device`` backed by a :class:`PulseBridgePoller`."""

    def __init__(
        self,
        device_id: str,
        category: DeviceCategory,
        poller: PulseBridgePoller,
        fallback: Any | None = None,
    ) -> None:
        self._device_id = device_id
        self._category = category
        self._poller = poller
        self._fallback = fallback

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return self._category

    async def get_state(self) -> DeviceState:
        self._poller.ensure_started()
        cloud = await self._fallback.get_state() if self._fallback is not None else None

        reading = self._poller.latest()
        if reading is None:
            if cloud is not None and cloud.available:
                return cloud.model_copy(
                    update={"device_id": self._device_id, "extra": {**cloud.extra, "source": "cloud"}}
                )
            return DeviceState(device_id=self._device_id, power_w=None, available=False)

        extra: dict[str, Any] = {
            "source": "local",
            "import_w": max(reading.power_w, 0.0),
            "export_w": max(-reading.power_w, 0.0),
            **reading.phases,
        }
        if reading.export_kwh is not None:
            extra["meter_production_kwh"] = reading.export_kwh
        if reading.frequency_hz is not None:
            extra["frequency_hz"] = reading.frequency_hz
        node = self._poller.node
        if node is not None and node.battery_voltage is not None:
            extra["pulse_battery_v"] = node.battery_voltage
        return DeviceState(
            device_id=self._device_id,
            timestamp=reading.timestamp,
            power_w=reading.power_w,
            energy_kwh=reading.import_kwh,
            available=True,
            extra=extra,
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """No-op — the meter is read-only."""

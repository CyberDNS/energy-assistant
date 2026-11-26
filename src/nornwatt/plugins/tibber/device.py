"""TibberDevice — live meter power from the Tibber Pulse/Watty stream.

``power_w`` is the net power at the meter: ``power - powerProduction``
(positive = importing, negative = exporting), matching the platform sign
convention.  ``energy_kwh`` is the meter's total import counter
(``lastMeterConsumption``) when the Pulse reports it.

The websocket is started on the first ``get_state()`` call; until the first
measurement arrives, or when the stream has been silent for a minute, the
device reports ``available=False``.
"""

from __future__ import annotations

from ...core.device_model import DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .api import LiveStream


class TibberDevice:
    """A read-only meter ``Device`` backed by the Tibber live stream."""

    def __init__(self, device_id: str, category: DeviceCategory, stream: LiveStream) -> None:
        self._device_id = device_id
        self._category = category
        self._stream = stream

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return self._category

    async def get_state(self) -> DeviceState:
        self._stream.ensure_started()
        m = self._stream.latest()
        if m is None:
            return DeviceState(device_id=self._device_id, power_w=None, available=False)
        extra: dict[str, float] = {"import_w": m.power_w, "export_w": m.production_w}
        if m.accumulated_consumption_kwh is not None:
            extra["accumulated_consumption_today_kwh"] = m.accumulated_consumption_kwh
        if m.meter_production_kwh is not None:
            extra["meter_production_kwh"] = m.meter_production_kwh
        return DeviceState(
            device_id=self._device_id,
            timestamp=m.timestamp,
            power_w=m.power_w - m.production_w,
            energy_kwh=m.meter_consumption_kwh,
            available=True,
            extra=extra,
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """No-op — the Tibber meter is read-only."""

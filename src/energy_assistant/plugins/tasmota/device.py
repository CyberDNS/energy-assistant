"""TasmotaDevice — power read from a Tasmota device on the LAN.

``power_w`` is the mapped power (positive = consuming / importing, negative =
producing / exporting).  ``energy_kwh`` is the import / consumption counter.
``extra`` carries the export counter (``meter_production_kwh``), per-phase
values (``l1_power_w`` …), ``voltage_v`` / ``current_a`` and any
``fields.extra`` values; meters also get ``import_w`` / ``export_w``.
"""

from __future__ import annotations

from typing import Any

from ...core.models import DeviceCommand, DeviceRole, DeviceState
from .poller import TasmotaPoller


class TasmotaDevice:
    """A read-only ``Device`` backed by a :class:`TasmotaPoller`."""

    def __init__(self, device_id: str, role: DeviceRole, poller: TasmotaPoller) -> None:
        self._device_id = device_id
        self._role = role
        self._poller = poller

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

    async def get_state(self) -> DeviceState:
        self._poller.ensure_started()
        reading = self._poller.latest()
        if reading is None:
            return DeviceState(device_id=self._device_id, power_w=None, available=False)

        extra: dict[str, Any] = {"source": "tasmota", **reading.values}
        if self._role == DeviceRole.METER:
            extra["import_w"] = max(reading.power_w, 0.0)
            extra["export_w"] = max(-reading.power_w, 0.0)
        if reading.energy_out_kwh is not None:
            extra["meter_production_kwh"] = reading.energy_out_kwh
        return DeviceState(
            device_id=self._device_id,
            timestamp=reading.timestamp,
            power_w=reading.power_w,
            energy_kwh=reading.energy_in_kwh,
            available=True,
            extra=extra,
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """No-op — read-only for now (relay switching is a later step)."""

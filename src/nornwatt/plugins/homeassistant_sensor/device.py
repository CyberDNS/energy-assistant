"""HASensorDevice — readings of a meter, PV system, consumer or sensor from HA.

State reported
--------------
``power_w``         the ``power_w`` binding (> 0 consuming / importing)
``energy_kwh``      the ``energy_in_kwh`` binding
``extra``           ``import_w`` / ``export_w`` (import/export power binding),
                    ``energy_in_kwh``, ``energy_out_kwh``, ``phase_power_w``
                    (list), ``temperature_c`` and ``measured_value`` (the
                    temperature)
``available``       the main reading is there: power when bound, else the
                    temperature, else any bound value

Read only: ``send_command`` is a no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability, DeviceCategory
from ...core.models import DeviceCommand, DeviceState
from .._homeassistant.bindings import ReadBinding

_log = logging.getLogger(__name__)


class HASensorDevice:
    """A read-only ``Device`` whose capabilities are bound to HA entities."""

    def __init__(
        self,
        device_id: str,
        category: DeviceCategory,
        client: Any,
        bindings: dict[Capability, ReadBinding],
    ) -> None:
        self._device_id = device_id
        self._category = category
        self._client = client
        self._bindings = dict(bindings)

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return self._category

    @property
    def bindings(self) -> dict[Capability, ReadBinding]:
        return dict(self._bindings)

    async def get_state(self) -> DeviceState:
        values: dict[Capability, Any] = {}
        extra: dict[str, Any] = {}
        for cap, binding in self._bindings.items():
            values[cap] = (await binding.read(self._client)).value
            if cap is Capability.POWER_W and binding.import_entity:
                imp, exp = await binding.read_pair(self._client)
                if imp is not None and exp is not None:
                    extra["import_w"], extra["export_w"] = imp, exp
        for cap, key in ((Capability.ENERGY_IN_KWH, "energy_in_kwh"),
                         (Capability.ENERGY_OUT_KWH, "energy_out_kwh"),
                         (Capability.PHASE_POWER_W, "phase_power_w"),
                         (Capability.TEMPERATURE_C, "temperature_c")):
            if cap in values:
                extra[key] = values[cap]
        if Capability.TEMPERATURE_C in values:
            extra["measured_value"] = values[Capability.TEMPERATURE_C]
        if Capability.POWER_W in self._bindings:
            available = values[Capability.POWER_W] is not None
        elif Capability.TEMPERATURE_C in self._bindings:
            available = values[Capability.TEMPERATURE_C] is not None
        else:
            available = any(v is not None for v in values.values())
        return DeviceState(
            device_id=self._device_id,
            power_w=values.get(Capability.POWER_W),
            energy_kwh=values.get(Capability.ENERGY_IN_KWH),
            available=available,
            extra=extra,
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """Read only — commands are ignored."""

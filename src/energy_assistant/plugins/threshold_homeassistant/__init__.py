"""threshold_homeassistant plugin — sensor reader + switch controller via HA.

Device type: ``threshold_homeassistant``

Configuration
-------------
``capabilities.temperature_c.entity``  — HA entity of the measured value
                                         (temperature, humidity, …).
``capabilities.switch.entity``         — HA switch entity controlling the load.
``params.rated_power_w``               — power drawn while running (W).

Example::

    - id: aquarium_cooler
      category: thermal_load
      type: threshold_homeassistant
      capabilities:
        temperature_c: { entity: sensor.aquarium_thermometer_temperature }
        switch: { entity: switch.aquarium_cooler_plug }
      params: { rated_power_w: 150 }

The switch is bound on the thermal load itself for now; it becomes a
``switch`` device the thermal load is ``controlled_by`` later.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import DeviceConfigError
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "threshold_homeassistant", _build,
        categories={C.THERMAL_LOAD},
        provides={Cap.POWER_W},
        bindable={Cap.TEMPERATURE_C, Cap.SWITCH},
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import ThresholdHADevice

    if ctx.ha_client is None:
        _log.warning(
            "Device %r (threshold_homeassistant) requires Home Assistant backend — skipping",
            device_id,
        )
        return None

    caps = cfg.get("capabilities") or {}
    entity_sensor = (caps.get("temperature_c") or {}).get("entity")
    entity_switch = (caps.get("switch") or {}).get("entity")
    rated_power_w = cfg.get("rated_power_w")

    if not entity_sensor or not entity_switch or rated_power_w is None:
        raise DeviceConfigError([(
            f"device {device_id!r} (threshold_homeassistant) needs "
            "'capabilities: {temperature_c: {entity: …}, switch: {entity: …}}' and "
            "'params: {rated_power_w: …}'"
        )])

    return ThresholdHADevice(
        device_id=device_id,
        client=ctx.ha_client,
        entity_sensor=str(entity_sensor),
        entity_switch=str(entity_switch),
        rated_power_w=float(rated_power_w),
    )

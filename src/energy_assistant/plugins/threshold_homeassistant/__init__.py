"""threshold_homeassistant plugin — sensor reader + switch controller via HA.

Device type: ``threshold_homeassistant``

Configuration fields
--------------------
``entity_sensor``  — HA entity ID for the measured value (temperature, humidity, …).
``entity_switch``  — HA entity ID for the on/off switch controlling the load.
``rated_power_w``  — Electrical power drawn when running (W).

Example::

    - id: aquarium_cooler
      type: threshold_homeassistant
      role: threshold_controlled
      entity_sensor: sensor.aquarium_thermometer_temperature
      entity_switch: switch.nous_plug_14_aquarium_cooler
      rated_power_w: 150
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device("threshold_homeassistant", _build)


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import ThresholdHADevice

    if ctx.ha_client is None:
        _log.warning(
            "Device %r (threshold_homeassistant) requires Home Assistant backend — skipping",
            device_id,
        )
        return None

    entity_sensor = cfg.get("entity_sensor")
    entity_switch = cfg.get("entity_switch")
    rated_power_w = cfg.get("rated_power_w")

    if not entity_sensor or not entity_switch or rated_power_w is None:
        _log.warning(
            "Device %r (threshold_homeassistant): missing required fields "
            "(entity_sensor, entity_switch, rated_power_w) — skipping",
            device_id,
        )
        return None

    return ThresholdHADevice(
        device_id=device_id,
        client=ctx.ha_client,
        entity_sensor=str(entity_sensor),
        entity_switch=str(entity_switch),
        rated_power_w=float(rated_power_w),
    )

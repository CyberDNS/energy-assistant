"""homeassistant_switch plugin — an on/off switch (plug, relay) in Home Assistant.

Device type: ``homeassistant_switch``   (category ``switch``)

Configuration
-------------
``capabilities.switch.entity``   — HA entity switched on/off (``switch.*``,
                                   ``input_boolean.*``, …).
``capabilities.power_w.entity``  — optional measured power of the plug (W).
``params.rated_power_w``         — power the load draws while on (W).  Used
                                   as the power reading when no power sensor
                                   is bound, and by the planner for the device
                                   this switch controls.

Example::

    - id: aquarium_cooler_plug
      category: switch
      type: homeassistant_switch
      capabilities:
        switch: { entity: switch.nous_plug_14_aquarium_cooler }
      params: { rated_power_w: 150 }

A switch executes the plan of the device that is ``controlled_by`` it (a
``thermal_load``); that device sends its commands through the switch.
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
        "homeassistant_switch", _build,
        title="Home Assistant switch",
        categories={C.SWITCH},
        bindable={Cap.SWITCH, Cap.POWER_W},
        settings={},
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import HASwitchDevice

    if ctx.ha_client is None:
        _log.warning(
            "Device %r (homeassistant_switch) requires Home Assistant backend — skipping",
            device_id,
        )
        return None

    caps = cfg.get("capabilities") or {}
    entity_switch = (caps.get("switch") or {}).get("entity")
    entity_power = (caps.get("power_w") or {}).get("entity")
    if not entity_switch:
        raise DeviceConfigError([(
            f"device {device_id!r} (homeassistant_switch) needs "
            "'capabilities: {switch: {entity: …}}'"
        )])
    rated = cfg.get("rated_power_w")
    return HASwitchDevice(
        device_id=device_id,
        client=ctx.ha_client,
        entity_switch=str(entity_switch),
        entity_power=str(entity_power) if entity_power else None,
        rated_power_w=float(rated) if rated is not None else None,
    )

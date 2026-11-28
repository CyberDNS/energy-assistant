"""homeassistant_thermal plugin — a thermal load whose value comes from HA.

Device type: ``homeassistant_thermal``   (category ``thermal_load``)

The thermal load reads its measured value (temperature, humidity, …) from a
Home Assistant entity and is switched by the ``switch`` device it is
``controlled_by`` (any switch plugin, e.g. ``homeassistant_switch``): its
power is the switch's, and its on/off commands go to the switch.

Configuration
-------------
``capabilities.temperature_c``         — HA entity of the measured value (any
                                         temperature binding, e.g. a climate
                                         entity's ``current_temperature``
                                         attribute).
``controlled_by``                      — the switch device (with
                                         ``params.rated_power_w``).
``params``                             — thresholds and the fallback thermal
                                         model, see the ``thermal_load``
                                         category (``core/device_model``).
``learning``                           — learned thermal model (default on);
                                         ``false`` keeps the configured rates,
                                         a mapping sets the learning options
                                         (``plugins/learned/thermal``).

Example::

    - id: aquarium_cooler
      category: thermal_load
      type: homeassistant_thermal
      capabilities:
        temperature_c: { entity: sensor.aquarium_thermometer_temperature }
      controlled_by: aquarium_cooler_plug
      params:
        bottom_threshold: 18.0
        top_threshold: 20.5
        active_rate_per_h: 0.417
        drift_rate_per_h: 0.185
        min_runtime_h: 0.5
        min_offtime_h: 0.5
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import DeviceConfigError
from ...core.plugin_registry import BuildContext, PluginRegistry
from .._homeassistant.bindings import binding_specs, parse_bindings

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    # Deferred: needs the switch device it is controlled_by.
    registry.register_device(
        "homeassistant_thermal", _build,
        title="Home Assistant temperature sensor",
        description="A cooler, fridge or heating rod with a temperature sensor in Home "
                    "Assistant, switched through a switch device.",
        deferred=True,
        categories={C.THERMAL_LOAD},
        provides={Cap.POWER_W},
        bindable={Cap.TEMPERATURE_C},
        bindings={Cap.TEMPERATURE_C: binding_specs(Cap.TEMPERATURE_C)},
        settings={},
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import ThermalLoadDevice

    readings = parse_bindings(device_id, "homeassistant_thermal", cfg, {Cap.TEMPERATURE_C})
    if Cap.TEMPERATURE_C not in readings:
        raise DeviceConfigError([(
            f"device {device_id!r} (homeassistant_thermal) needs "
            "'capabilities: {temperature_c: {entity: …}}'"
        )])
    if ctx.ha_client is None:
        _log.warning(
            "Device %r (homeassistant_thermal) requires Home Assistant backend — skipping",
            device_id,
        )
        return None
    switch_id = cfg.get("controlled_by")
    switch = ctx.device_registry.get(switch_id) if ctx.device_registry and switch_id else None
    if switch is None:
        _log.warning("Device %r (homeassistant_thermal): switch %r was not built — skipping",
                     device_id, switch_id)
        return None
    return ThermalLoadDevice(
        device_id=device_id,
        client=ctx.ha_client,
        sensor=readings[Cap.TEMPERATURE_C],
        switch=switch,
    )

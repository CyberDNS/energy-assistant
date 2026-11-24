"""homeassistant_thermostat plugin — a thermal load driven by a climate or water heater entity.

Device type: ``homeassistant_thermostat``   (category ``thermal_load``)

Like ``homeassistant_thermal`` but without a switch device: the thermostat is
told to run or stop (``control.py``), and the thermal load's thresholds, rates,
run times and learning work as for any thermal load.  It needs its own
``params.rated_power_w`` (the power while it runs).

Example — a heat pump's hot-water tank::

    - id: hot_water
      category: thermal_load
      type: homeassistant_thermostat
      capabilities:
        temperature_c: { entity: water_heater.heat_pump, attribute: current_temperature }
        power_setpoint:
          control: water_heater           # run → 55 °C, stop → 45 °C
          entity: water_heater.heat_pump
          normal: 50                      # restored when EA stops or its plan is stale
      params:
        bottom_threshold: 45
        top_threshold: 55
        direction: increases
        rated_power_w: 1800
        active_rate_per_h: 8
        drift_rate_per_h: 0.6
        min_runtime_h: 0.5

Example — a room thermostat by HVAC mode::

        temperature_c: { entity: climate.office, attribute: current_temperature }
        power_setpoint: { control: hvac_mode, entity: climate.office,
                          run_mode: heat, stop_mode: "off", normal: heat }

Learning needs the temperature's and the on/off state's history as entity
states: with an attribute binding set ``learning: {value_entity: …, modes:
{…}}`` (``plugins/learned/thermal``), or ``learning: false``.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import DeviceConfigError
from ...core.plugin_registry import BuildContext, PluginRegistry
from .._homeassistant.bindings import binding_specs, parse_bindings
from .control import POWER_SETPOINT_BINDING, parse_control

_log = logging.getLogger(__name__)

TYPE = "homeassistant_thermostat"
_READINGS = frozenset({Cap.TEMPERATURE_C, Cap.POWER_W})


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        TYPE, _build,
        title="Thermostat from Home Assistant",
        description="A heater, cooler or hot-water tank run and stopped through its climate "
                    "or water heater entity (target temperature, HVAC mode or preset).",
        categories={C.THERMAL_LOAD},
        bindable={*_READINGS, Cap.POWER_SETPOINT},
        bindings={
            **{cap: binding_specs(cap) for cap in _READINGS},
            Cap.POWER_SETPOINT: POWER_SETPOINT_BINDING,
        },
        settings={},
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import HAThermostatDevice

    where = f"device {device_id!r} ({TYPE})"
    readings = parse_bindings(device_id, TYPE, cfg, _READINGS)
    if Cap.TEMPERATURE_C not in readings:
        raise DeviceConfigError([f"{where} needs 'capabilities: {{temperature_c: {{entity: …}}}}'"])
    setpoint = (cfg.get("capabilities") or {}).get("power_setpoint")
    if not setpoint:
        raise DeviceConfigError([(f"{where} needs 'capabilities: {{power_setpoint: "
                                  "{control: …, entity: …}}' (how it is run and stopped)")])
    if cfg.get("controlled_by"):
        raise DeviceConfigError([(f"{where} switches itself: remove 'controlled_by' (a "
                                  "thermal load with a switch is type homeassistant_thermal)")])
    rated = cfg.get("rated_power_w")
    if rated is None:
        raise DeviceConfigError([f"{where} needs 'params: {{rated_power_w: …}}'"])
    if ctx.ha_client is None:
        _log.warning("Device %r (%s) requires Home Assistant backend — skipping",
                     device_id, TYPE)
        return None
    control = parse_control(
        f"device {device_id!r}", ctx.ha_client, setpoint,
        bottom=float(cfg["bottom_threshold"]), top=float(cfg["top_threshold"]),
        direction=str(cfg.get("direction", "reduces")),
    )
    return HAThermostatDevice(device_id, ctx.ha_client, readings, control, float(rated))

"""homeassistant_charger plugin — any EV charger Home Assistant can read and command.

Device type: ``homeassistant_charger``   (category ``charger``)

Readings: ``power_w`` and ``plugged`` (any HA binding; ``plugged`` takes an
``on:`` list for status sensors).  Control: the ``power_setpoint`` binding
(``control.py``).  Without it the charger is only read.

Examples::

    # go-e / Easee / Wallbox style: charging current + a charging switch
    - id: wallbox_garage
      category: charger
      type: homeassistant_charger
      phases: 3
      capabilities:
        power_w: { entity: sensor.wallbox_power }
        plugged: { entity: sensor.wallbox_status, on: [Charging, Wait car, Complete] }
        power_setpoint:
          control: current
          current: number.wallbox_max_current
          enable: switch.wallbox_charging
      params: { min_power_kw: 4.1, max_power_kw: 11 }

    # openWB style: charge mode select (the charger follows the PV surplus itself)
        power_setpoint:
          control: modes
          mode: select.openwb_chargepoint_lademodus
          modes:
            charge:         { option: Instant Charging, current: number.openwb_instant_current }
            absorb_surplus: { option: PV Charging }
            stop:           { option: Stop }
          soc_limit: number.openwb_instant_soc_limit
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import DeviceConfigError, ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry
from .._homeassistant.bindings import binding_specs, parse_bindings
from .control import POWER_SETPOINT_BINDING, parse_control

_log = logging.getLogger(__name__)

TYPE = "homeassistant_charger"
_READINGS = frozenset({Cap.POWER_W, Cap.PLUGGED})

SETTINGS: dict[str, ParamSpec] = {
    "phases": ParamSpec(int, minimum=1, maximum=3, default=3, label="Phases",
                        description="Number of phases the charger charges with. Used to "
                                    "convert between current and power"),
    "voltage_v": ParamSpec(float, minimum=0, exclusive_minimum=True, default=230.0,
                           advanced=True, label="Voltage (V)",
                           description="Voltage of one phase. Used to convert between "
                                       "current and power"),
}


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        TYPE, build,
        title="Charger from Home Assistant",
        description="Any EV charger whose power and plug state are in Home Assistant; "
                    "commanded through a current or power number, a script or its charge modes.",
        categories={C.CHARGER},
        bindable={*_READINGS, Cap.POWER_SETPOINT},
        bindings={
            **{cap: binding_specs(cap) for cap in _READINGS},
            Cap.POWER_SETPOINT: POWER_SETPOINT_BINDING,
        },
        settings=SETTINGS,
    )


def build(device_id: str, cfg: dict[str, Any], ctx: BuildContext, *,
          type_name: str = TYPE, require_power: bool = True) -> object | None:
    from .device import HAChargerDevice

    readings = parse_bindings(device_id, type_name, cfg, _READINGS)
    if require_power and Cap.POWER_W not in readings:
        raise DeviceConfigError([(f"device {device_id!r} ({type_name}) needs "
                                  "'capabilities: {power_w: {entity: …}}'")])
    if ctx.ha_client is None:
        _log.warning("Device %r (%s) requires Home Assistant backend — skipping",
                     device_id, type_name)
        return None
    setpoint = (cfg.get("capabilities") or {}).get("power_setpoint")
    control = (parse_control(f"device {device_id!r}", ctx.ha_client, setpoint,
                             phases=int(cfg.get("phases", 3)),
                             voltage_v=float(cfg.get("voltage_v", 230.0)))
               if setpoint else None)
    return HAChargerDevice(device_id, ctx.ha_client, readings, control)

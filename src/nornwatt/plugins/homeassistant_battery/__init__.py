"""homeassistant_battery plugin — any battery Home Assistant can read and command.

Device type: ``homeassistant_battery``   (category ``battery``)

Readings: ``power_w`` (+ = charging) and ``soc_pct``, as any HA binding
(``_homeassistant/bindings``).  Control: the ``power_setpoint`` binding, one
of five variants (``control.py``).  Without ``power_setpoint`` the battery is
only read (planned, but not commanded).

Examples::

    # A — one signed power number
    - id: home_battery
      category: battery
      type: homeassistant_battery
      capabilities:
        power_w: { entity: sensor.battery_power }
        soc_pct: { entity: sensor.battery_soc }
        power_setpoint: { control: number, number: number.battery_power_setpoint }
      params: { capacity_kwh: 10, max_charge_kw: 5, max_discharge_kw: 5 }

    # C — working mode + one power (e.g. Huawei)
        power_setpoint:
          control: mode_power
          mode: select.battery_working_mode
          power: number.battery_forcible_charge_power
          charge_option: Forcible charge
          discharge_option: Forcible discharge
          hold_option: Fixed charge/discharge
          release_option: Maximise self consumption
        # … or one power number per direction instead of `power`:
        #   charge_power: number.battery_forcible_charge_power
        #   discharge_power: number.battery_forcible_discharge_power

    # mode control — the battery follows surplus and load itself
        power_setpoint:
          control: modes
          mode: select.battery_working_mode
          modes:
            charge:           { option: Forcible charge, power: number.battery_forcible_charge_power }
            discharge:        { option: Forcible discharge, power: number.battery_forcible_discharge_power }
            self_consumption: { option: Maximise self consumption }
            absorb_surplus:   { option: Maximise self consumption,
                                set: { number.battery_max_discharge_power: 0 } }
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import DeviceConfigError
from ...core.plugin_registry import BuildContext, PluginRegistry
from .._homeassistant.bindings import CHARGE_DISCHARGE, binding_specs, parse_bindings
from .control import POWER_SETPOINT_BINDING, parse_control

_log = logging.getLogger(__name__)

TYPE = "homeassistant_battery"
_READINGS = frozenset({Cap.POWER_W, Cap.SOC_PCT})


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        TYPE, _build,
        title="Battery from Home Assistant",
        description="Any battery whose power and state of charge are in Home Assistant; "
                    "commanded through a power number, mode select or script.",
        categories={C.BATTERY},
        bindable={*_READINGS, Cap.POWER_SETPOINT},
        bindings={
            Cap.POWER_W: binding_specs(Cap.POWER_W, directions=CHARGE_DISCHARGE),
            Cap.SOC_PCT: binding_specs(Cap.SOC_PCT),
            Cap.POWER_SETPOINT: POWER_SETPOINT_BINDING,
        },
        settings={},
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import HABatteryDevice

    readings = parse_bindings(device_id, TYPE, cfg, _READINGS)
    missing = [c.value for c in (Cap.POWER_W, Cap.SOC_PCT) if c not in readings]
    if missing:
        raise DeviceConfigError([(
            f"device {device_id!r} ({TYPE}) needs {' and '.join(missing)} bound, e.g. "
            "'capabilities: {power_w: {entity: …}, soc_pct: {entity: …}}'"
        )])
    if ctx.ha_client is None:
        _log.warning("Device %r (%s) requires Home Assistant backend — skipping",
                     device_id, TYPE)
        return None
    setpoint = (cfg.get("capabilities") or {}).get("power_setpoint")
    control = (parse_control(f"device {device_id!r}", ctx.ha_client, setpoint)
               if setpoint else None)
    return HABatteryDevice(device_id, ctx.ha_client, readings, control)

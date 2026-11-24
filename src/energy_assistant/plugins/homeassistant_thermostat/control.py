"""How a ``homeassistant_thermostat`` runs and stops: the ``power_setpoint`` binding.

The thermal load's plan is on/off (run / standby, ``assets/threshold.py``).  A
thermostat is told to run or stop through one of:

``control: target_temperature``  move the climate entity's target temperature:
                                  run → ``run_temperature``, stop →
                                  ``stop_temperature`` (default: a heater runs
                                  towards the top threshold and stops at the
                                  bottom one, a cooler the other way round)
``control: hvac_mode``           ``run_mode`` / ``stop_mode`` (e.g. heat / off)
``control: preset``              ``run_preset`` / ``stop_preset`` (e.g. boost / eco)
``control: water_heater``        a water heater's target temperature
                                  (``run_temperature`` / ``stop_temperature``)
``control: water_heater_operation``  its ``run_operation`` / ``stop_operation``

``normal``: the value of the user's own operation (a temperature, mode,
preset or operation).  It is restored when Energy Assistant stops and when no
command came for ``stale_after_min`` (a plan that is no longer executed must
not leave the heating forced on or off).
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import DeviceConfigError, ParamSpec
from .._homeassistant.actuators import (
    ServiceActuator,
    climate_actuator,
    water_heater_actuator,
)

_log = logging.getLogger(__name__)

CONTROLS = ("target_temperature", "hvac_mode", "preset", "water_heater",
            "water_heater_operation")
#: control → (what is written, keys of the run and stop values, numeric)
_VARIANTS: dict[str, tuple[str, str, str, bool]] = {
    "target_temperature": ("temperature", "run_temperature", "stop_temperature", True),
    "hvac_mode": ("hvac_mode", "run_mode", "stop_mode", False),
    "preset": ("preset", "run_preset", "stop_preset", False),
    "water_heater": ("temperature", "run_temperature", "stop_temperature", True),
    "water_heater_operation": ("operation_mode", "run_operation", "stop_operation", False),
}
DEFAULT_RUNNING = ("heating", "cooling", "drying", "preheating", "defrosting")


def _when(*controls: str) -> dict[str, Any]:
    return {"control": controls if len(controls) > 1 else controls[0]}


def _text(label: str, description: str, *controls: str) -> ParamSpec:
    return ParamSpec(str, required=True, label=label, description=description,
                     when=_when(*controls))


#: Keys of the ``power_setpoint`` binding (validation and config UI).
POWER_SETPOINT_BINDING: dict[str, ParamSpec] = {
    "control": ParamSpec(str, required=True, choices=CONTROLS, label="Run and stop through",
                         description="target_temperature · hvac_mode · preset (climate "
                                     "entity) · water_heater (temperature) · "
                                     "water_heater_operation"),
    "entity": ParamSpec(str, required=True, entity=True, domains=("climate", "water_heater"),
                        label="Thermostat", description="Climate or water heater entity"),
    "run_temperature": ParamSpec(float, label="Run at", when=_when("target_temperature",
                                                                  "water_heater"),
                                 description="Target while running (default: the threshold "
                                             "it runs towards)"),
    "stop_temperature": ParamSpec(float, label="Stop at", when=_when("target_temperature",
                                                                    "water_heater"),
                                  description="Target while stopped (default: the other "
                                              "threshold)"),
    "run_mode": _text("Run mode", "HVAC mode while running, e.g. heat", "hvac_mode"),
    "stop_mode": _text("Stop mode", "HVAC mode while stopped, e.g. off", "hvac_mode"),
    "run_preset": _text("Run preset", "Preset while running, e.g. boost", "preset"),
    "stop_preset": _text("Stop preset", "Preset while stopped, e.g. eco", "preset"),
    "run_operation": _text("Run operation", "Operation mode while running, e.g. "
                           "high_demand", "water_heater_operation"),
    "stop_operation": _text("Stop operation", "Operation mode while stopped, e.g. eco",
                            "water_heater_operation"),
    "normal": ParamSpec(object, label="Normal operation",
                        description="Your own temperature / mode / preset, restored when "
                                    "Energy Assistant stops or its plan is stale"),
    "running_states": ParamSpec(list, items=ParamSpec(str), advanced=True,
                                label="Running hvac_action states",
                                description="hvac_action values that mean it runs "
                                            "(default: heating, cooling, …)"),
    "stale_after_min": ParamSpec(float, default=30.0, minimum=1, advanced=True,
                                 label="Restore after (min)",
                                 description="Restore the normal operation when no command "
                                             "came for this long"),
}


class ThermostatControl:
    """Run / stop a thermostat (see module docstring)."""

    def __init__(
        self,
        where: str,
        control: str,
        actuator: ServiceActuator,
        run_value: Any,
        stop_value: Any,
        *,
        normal: Any = None,
        running_states: tuple[str, ...] = DEFAULT_RUNNING,
        stale_after_s: float = 1800.0,
    ) -> None:
        self.where = where
        self.control = control
        self.actuator = actuator
        self.run_value = run_value
        self.stop_value = stop_value
        self.normal = normal
        self.running_states = frozenset(s.lower() for s in running_states)
        self.stale_after_s = stale_after_s

    @property
    def entity_id(self) -> str:
        return self.actuator.entity_id

    @property
    def actuators(self) -> list[Any]:
        return [self.actuator]

    async def apply(self, run: bool) -> None:
        await self.actuator.apply(self.run_value if run else self.stop_value)

    async def restore(self) -> bool:
        """Back to the normal operation (when configured).  True when it wrote."""
        if self.normal is None:
            return False
        return await self.actuator.apply(self.normal)


def parse_control(
    where: str, client: Any, raw: Any, *,
    bottom: float, top: float, direction: str,
) -> ThermostatControl:
    """A ``ThermostatControl`` from the ``power_setpoint`` binding and the
    thermal load's thresholds."""
    if not isinstance(raw, dict):
        raise DeviceConfigError([f"{where}: must be a mapping with 'control: …'"])
    control = raw.get("control")
    if control not in CONTROLS:
        raise DeviceConfigError([(f"{where}: control must be one of {', '.join(CONTROLS)}, "
                                  f"got {control!r}")])
    entity = raw.get("entity")
    if not isinstance(entity, str) or "." not in entity:
        raise DeviceConfigError([f"{where}: needs 'entity: <climate or water_heater entity>'"])
    domain = entity.split(".", 1)[0]
    what, run_key, stop_key, numeric = _VARIANTS[control]
    wanted_domain = "water_heater" if control.startswith("water_heater") else "climate"
    if domain != wanted_domain:
        raise DeviceConfigError([(f"{where}: control {control} needs a {wanted_domain} "
                                  f"entity, got {entity!r}")])
    heater = direction == "increases"
    run_value, stop_value = raw.get(run_key), raw.get(stop_key)
    if numeric:
        run_value = float(run_value if run_value is not None else (top if heater else bottom))
        stop_value = float(stop_value if stop_value is not None else (bottom if heater else top))
    elif not run_value or not stop_value:
        raise DeviceConfigError([(f"{where}: control {control} needs '{run_key}' and "
                                  f"'{stop_key}'")])
    actuator = (water_heater_actuator if wanted_domain == "water_heater" else climate_actuator)(
        where, client, entity, what)
    normal = raw.get("normal")
    if normal is not None and numeric and not isinstance(normal, (int, float)):
        raise DeviceConfigError([f"{where}: normal must be a temperature for control {control}"])
    return ThermostatControl(
        where, control, actuator, run_value, stop_value,
        normal=float(normal) if normal is not None and numeric else normal,
        running_states=tuple(str(s) for s in raw.get("running_states") or DEFAULT_RUNNING),
        stale_after_s=float(raw.get("stale_after_min", 30.0)) * 60.0,
    )

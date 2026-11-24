"""How a ``homeassistant_charger`` is commanded: the ``power_setpoint`` binding.

The control loop sends a watt value and a behaviour (``core.models.Behaviour``):
``charge`` at the power, ``absorb_surplus`` (the watt value is EA's estimate
of the PV surplus the charger could take) or ``stop``.

Setpoint control — EA decides the power every tick:

``control: current``   a charging-current number (A) and an optional ``enable``
                        switch; W → A with the charger's ``phases`` × ``voltage_v``
``control: power``     a power number (W or kW) and an optional ``enable`` switch
``control: action``    an action with the power in its data (0 = stop)

Below ``min_current_a`` the charger is switched off (``enable`` off, or the
number to 0 without a switch).  Following the surplus, a charger that is
charging keeps the minimum current for ``stop_delay_s`` before it stops, so a
passing cloud does not switch it off and on.

Mode control — the charger follows the surplus itself:

``control: modes``     a ``mode`` select and ``modes: {charge | absorb_surplus |
                        stop: {option, current, power, set}}``
    ``current`` / ``power``  get the commanded power (as A or W) in that mode
    ``set``                  fixed values to write with it, ``{entity: value}``

Any variant: ``soc_limit`` — a number that gets the target SoC (force charge,
the goal's target) whenever the charger is told to charge.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ...core.device_model import DeviceConfigError, ParamSpec
from ...core.models import BEHAVIOUR_LABELS, Behaviour
from .._homeassistant.actuators import (
    NUMBER_DOMAINS,
    SELECT_DOMAINS,
    SWITCH_DOMAINS,
    ActionActuator,
    NumberActuator,
    SelectActuator,
    SwitchActuator,
)

_log = logging.getLogger(__name__)

CONTROLS = ("current", "power", "action", "modes")
_POWER, _CURRENT, _PERCENT = ("W", "kW"), ("A",), ("%",)
CONTROL_LABELS = {
    "current": "Charging current (A)",
    "power": "Charging power",
    "action": "A script or action",
    "modes": "The charger's own modes (PV / instant / stop)",
}
_MODE_LABELS = {**BEHAVIOUR_LABELS, "charge": "Charge (instant)",
                "absorb_surplus": "PV surplus only"}
_BEHAVIOURS = (Behaviour.CHARGE.value, Behaviour.ABSORB_SURPLUS.value, Behaviour.STOP.value)


def _when(*controls: str) -> dict[str, Any]:
    return {"control": controls if len(controls) > 1 else controls[0]}


_MODE_ENTRY = ParamSpec(dict, fields={
    "option": ParamSpec(str, required=True, label="Option", options_from=("mode", "options"), 
                        description="Option of the mode select"),
    "current": ParamSpec(str, entity=True, domains=NUMBER_DOMAINS, units=_CURRENT,
                         label="Current number",
                         description="Gets the commanded power as charging current (A)"),
    "power": ParamSpec(str, entity=True, domains=NUMBER_DOMAINS, units=_POWER,
                       label="Power number",
                       description="Gets the commanded power (W or kW)"),
    "set": ParamSpec(dict, values=ParamSpec(object), label="Also set",
                     keys=ParamSpec(str, entity=True, domains=NUMBER_DOMAINS + SELECT_DOMAINS),
                     description="Fixed values written with the mode: {entity: value}"),
})

#: Keys of the ``power_setpoint`` binding (validation and config UI).
POWER_SETPOINT_BINDING: dict[str, ParamSpec] = {
    "control": ParamSpec(str, required=True, choices=CONTROLS, choice_labels=CONTROL_LABELS,
                         label="Controlled through",
                         description="How Energy Assistant tells the charger what to do"),
    "current": ParamSpec(str, required=True, entity=True, domains=NUMBER_DOMAINS,
                         units=_CURRENT, label="Current number", when=_when("current"),
                         description="Charging current (A)"),
    "power": ParamSpec(str, required=True, entity=True, domains=NUMBER_DOMAINS,
                       units=_POWER, label="Power number", when=_when("power"),
                       description="Charging power (W or kW)"),
    "enable": ParamSpec(str, entity=True, domains=SWITCH_DOMAINS, label="Charging switch",
                        when=_when("current", "power"),
                        description="Switch that starts and stops charging (without it the "
                                    "number is set to 0 to stop)"),
    "action": ParamSpec(str, required=True, label="Action", when=_when("action"),
                        description="Script or action called with the power, e.g. "
                                    "script.set_wallbox_power (0 = stop)"),
    "data": ParamSpec(dict, values=ParamSpec(object), advanced=True, label="Extra data",
                      when=_when("action"), description="More data for the action"),
    "power_key": ParamSpec(str, default="power_w", advanced=True, label="Power variable",
                           when=_when("action"), description="Data key that gets the power in W"),
    "mode": ParamSpec(str, required=True, entity=True, domains=SELECT_DOMAINS,
                      label="Mode select", when=_when("modes"),
                      description="Select entity of the charge mode"),
    "modes": ParamSpec(dict, required=True, label="Modes", when=_when("modes"),
                       values=_MODE_ENTRY,
                       keys=ParamSpec(str, choices=_BEHAVIOURS, choice_labels=_MODE_LABELS),
                       description="Per behaviour (charge, absorb_surplus, stop): option, "
                                   "current or power, set"),
    "release": ParamSpec(str, label="On stop", choices=_BEHAVIOURS, choice_labels=_MODE_LABELS,
                         when=_when("modes"),
                         description="Mode set when Energy Assistant stops (default: none)"),
    "soc_limit": ParamSpec(str, entity=True, domains=NUMBER_DOMAINS, units=_PERCENT,
                           label="SoC limit number",
                           description="Gets the target SoC when charging is commanded"),
    "min_current_a": ParamSpec(float, default=6.0, minimum=0, advanced=True,
                               label="Lowest current (A)",
                               description="Below this the charger is stopped"),
    "max_current_a": ParamSpec(float, default=16.0, minimum=0, exclusive_minimum=True,
                               advanced=True, label="Highest current (A)"),
    "stop_delay_s": ParamSpec(float, default=60.0, minimum=0, advanced=True,
                              label="Stop delay (s)",
                              description="Following the surplus: keep the lowest current "
                                          "this long before stopping"),
}


@dataclass
class _Mode:
    option: str
    current: NumberActuator | None = None
    power: NumberActuator | None = None
    fixed: list[tuple[Any, Any]] = field(default_factory=list)


class ChargerControl:
    """Turns charger commands (watt value + behaviour) into writes."""

    def __init__(
        self,
        where: str,
        control: str,
        *,
        phases: int = 3,
        voltage_v: float = 230.0,
        min_current_a: float = 6.0,
        max_current_a: float = 16.0,
        stop_delay_s: float = 60.0,
        current: NumberActuator | None = None,
        power: NumberActuator | None = None,
        enable: SwitchActuator | None = None,
        action: ActionActuator | None = None,
        mode: SelectActuator | None = None,
        modes: dict[Behaviour, _Mode] | None = None,
        release: Behaviour | None = None,
        soc_limit: NumberActuator | None = None,
    ) -> None:
        self.where = where
        self.control = control
        self.phases = phases
        self.voltage_v = voltage_v
        self.min_current_a = min_current_a
        self.max_current_a = max_current_a
        self.stop_delay_s = stop_delay_s
        self._current = current
        self._power = power
        self._enable = enable
        self._action = action
        self._mode = mode
        self._modes = modes or {}
        self._release = release
        self._soc_limit = soc_limit
        self._target_soc: float | None = None
        self._charging = False
        self._below_since: float | None = None
        self._warned: set[Behaviour] = set()
        self._clock = time.monotonic

    @property
    def supported_behaviours(self) -> frozenset[Behaviour] | None:
        return frozenset(self._modes) if self.control == "modes" else None

    @property
    def actuators(self) -> list[Any]:
        out: list[Any] = [a for a in (self._current, self._power, self._enable, self._action,
                                      self._mode, self._soc_limit) if a is not None]
        for m in self._modes.values():
            out.extend(a for a in (m.current, m.power) if a is not None)
            out.extend(a for a, _v in m.fixed)
        unique: dict[str, Any] = {}
        for a in out:
            unique.setdefault(a.entity_id, a)
        return list(unique.values())

    def update_target_soc(self, soc_pct: float | None) -> None:
        self._target_soc = soc_pct

    def _amps(self, power_w: float) -> float:
        return power_w / (self.voltage_v * self.phases)

    def _watts(self, amps: float) -> float:
        return amps * self.voltage_v * self.phases

    async def apply(self, power_w: float, behaviour: Behaviour) -> None:
        if behaviour is Behaviour.CHARGE:
            await self._write_soc_limit()
        if self.control == "modes":
            await self._apply_mode(power_w, behaviour)
            return
        target_w = 0.0 if behaviour is Behaviour.STOP else max(0.0, power_w)
        amps = min(self._amps(target_w), self.max_current_a)
        if amps < self.min_current_a:
            now = self._clock()
            if behaviour is Behaviour.ABSORB_SURPLUS and self._charging and target_w > 0:
                # Following the surplus: hold the minimum for stop_delay_s.
                if self._below_since is None:
                    self._below_since = now
                if now - self._below_since < self.stop_delay_s:
                    amps = self.min_current_a
            if amps < self.min_current_a:
                await self._stop()
                return
        else:
            self._below_since = None
        if self.control == "action":
            assert self._action is not None
            await self._action.apply(round(self._watts(amps)))
        elif self._current is not None:
            await self._current.apply(round(amps))
        elif self._power is not None:
            await self._power.apply(self._watts(amps))
        if self._enable is not None:
            await self._enable.apply(True)
        self._charging = True

    async def _stop(self) -> None:
        self._below_since = None
        if self.control == "action":
            assert self._action is not None
            await self._action.apply(0.0)
        elif self._enable is not None:
            await self._enable.apply(False)
        elif self._current is not None:
            await self._current.apply(0.0)
        elif self._power is not None:
            await self._power.apply(0.0)
        self._charging = False

    async def _apply_mode(self, power_w: float, behaviour: Behaviour) -> None:
        mode = self._modes.get(behaviour)
        if mode is None:
            if behaviour not in self._warned:
                self._warned.add(behaviour)
                _log.warning("%s: no mode for %s — commands skipped", self.where, behaviour.value)
            return
        await self._set_mode(mode, power_w)

    async def _set_mode(self, mode: _Mode, power_w: float | None) -> None:
        # Values first, then the mode: the charger never starts with stale ones.
        if power_w is not None and power_w > 0:
            if mode.current is not None:
                amps = max(self.min_current_a, min(self.max_current_a, self._amps(power_w)))
                await mode.current.apply(round(amps))
            if mode.power is not None:
                await mode.power.apply(power_w)
        for actuator, value in mode.fixed:
            await actuator.apply(value)
        assert self._mode is not None
        await self._mode.apply(mode.option)

    async def _write_soc_limit(self) -> None:
        if self._soc_limit is not None and self._target_soc is not None:
            await self._soc_limit.apply(float(round(self._target_soc)))

    async def release(self) -> None:
        if self.control == "modes" and self._release is not None:
            mode = self._modes.get(self._release)
            if mode is not None:
                await self._set_mode(mode, None)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_control(
    where: str, client: Any, raw: Any, *, phases: int = 3, voltage_v: float = 230.0,
) -> ChargerControl:
    """A ``ChargerControl`` from the ``power_setpoint`` binding."""
    if not isinstance(raw, dict):
        raise DeviceConfigError([f"{where}: must be a mapping with 'control: …'"])
    control = raw.get("control")
    if control not in CONTROLS:
        raise DeviceConfigError([(f"{where}: control must be one of {', '.join(CONTROLS)}, "
                                  f"got {control!r}")])
    actuators: dict[str, Any] = {}

    def need(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise DeviceConfigError([f"{where}: control {control} needs '{key}'"])
        return value

    def number(entity: str, *, power: bool = False, tolerance: float = 0.5) -> NumberActuator:
        if entity not in actuators:
            actuators[entity] = NumberActuator(where, client, entity, power=power,
                                               tolerance=tolerance)
        return actuators[entity]

    def select(entity: str) -> SelectActuator:
        if entity not in actuators:
            actuators[entity] = SelectActuator(where, client, entity)
        return actuators[entity]

    common: dict[str, Any] = {
        "phases": phases, "voltage_v": voltage_v,
        "min_current_a": float(raw.get("min_current_a", 6.0)),
        "max_current_a": float(raw.get("max_current_a", 16.0)),
        "stop_delay_s": float(raw.get("stop_delay_s", 60.0)),
        "soc_limit": number(str(raw["soc_limit"])) if raw.get("soc_limit") else None,
    }
    enable = SwitchActuator(where, client, str(raw["enable"])) if raw.get("enable") else None
    if control == "current":
        return ChargerControl(where, control, current=number(need("current")), enable=enable,
                              **common)
    if control == "power":
        return ChargerControl(where, control, power=number(need("power"), power=True,
                                                           tolerance=50.0),
                              enable=enable, **common)
    if control == "action":
        return ChargerControl(where, control, action=ActionActuator(
            where, client, need("action"), data=raw.get("data") or {},
            value_key=str(raw.get("power_key") or "power_w"), tolerance=50.0), **common)
    mode_select = select(need("mode"))
    modes_raw = raw.get("modes")
    if not isinstance(modes_raw, dict) or not modes_raw:
        raise DeviceConfigError([(f"{where}: control modes needs "
                                  "'modes: {<behaviour>: {option: …}}'")])
    errors: list[str] = []
    modes: dict[Behaviour, _Mode] = {}
    for name, entry in modes_raw.items():
        if name not in _BEHAVIOURS:
            errors.append(f"{where}: modes.{name}: must be one of {', '.join(_BEHAVIOURS)}")
            continue
        if not isinstance(entry, dict) or not entry.get("option"):
            errors.append(f"{where}: modes.{name}: needs 'option: <select option>'")
            continue
        fixed: list[tuple[Any, Any]] = []
        for entity, value in (entry.get("set") or {}).items():
            domain = str(entity).split(".", 1)[0]
            if domain in SELECT_DOMAINS:
                fixed.append((select(str(entity)), str(value)))
            elif domain in NUMBER_DOMAINS and isinstance(value, (int, float)):
                fixed.append((number(str(entity)), float(value)))
            else:
                errors.append(f"{where}: modes.{name}.set: {entity}: needs a number or "
                              "select entity with a number / option")
        modes[Behaviour(name)] = _Mode(
            str(entry["option"]),
            current=number(str(entry["current"])) if entry.get("current") else None,
            power=(number(str(entry["power"]), power=True, tolerance=50.0)
                   if entry.get("power") else None),
            fixed=fixed,
        )
    release = Behaviour(raw["release"]) if raw.get("release") else None
    if release is not None and release not in modes:
        errors.append(f"{where}: release {release.value!r} has no mode in 'modes'")
    if errors:
        raise DeviceConfigError(errors)
    return ChargerControl(where, control, mode=mode_select, modes=modes, release=release,
                          **common)

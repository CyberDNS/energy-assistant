"""How a ``homeassistant_battery`` is commanded: the ``power_setpoint`` binding.

Setpoint control (EA sends a watt value every tick, + = charge):

``control: number``            A — one signed power number
                                ``number``, ``invert_sign``
``control: charge_discharge``  B — a charge power and a discharge power number
                                ``charge``, ``discharge``
``control: mode_power``        C — a mode select and one unsigned power number
                                ``mode``, ``power``, ``charge_option``,
                                ``discharge_option``, ``hold_option``,
                                ``release_option``
``control: action``            D — an action with the power in its data
                                ``action``, ``data``, ``power_key``

Mode control (EA picks the mode for the behaviour, the battery follows
surplus and load itself; see ``core.models.Behaviour``):

``control: modes``             ``mode`` and ``modes: {<behaviour>: {option,
                                power, set}}``, ``release``
    ``option``  the select option for the behaviour
    ``power``   a power number that gets the commanded |W| (charge/discharge)
    ``set``     fixed values to write with it, ``{entity: value}`` (numbers
                to number entities, text to select entities), e.g. a
                discharge limit of 0 for ``absorb_surplus``

Behaviours the battery has no mode for: the control loop first narrows
them to ``charge`` / ``discharge`` / ``hold`` by the watt value
(``resolve_behaviour``); if there is no mode for that either, the closest
one is used (``_FALLBACK``: hold → absorb_surplus → charge at 0 W,
discharge → self_consumption, charge → absorb_surplus).

Power numbers in kW are converted.  Writes go through the shared actuators
(write on mismatch, rate limit, read-back health).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ...core.device_model import DeviceConfigError, ParamSpec
from ...core.models import Behaviour
from .._homeassistant.actuators import (
    NUMBER_DOMAINS,
    SELECT_DOMAINS,
    ActionActuator,
    NumberActuator,
    SelectActuator,
)

_log = logging.getLogger(__name__)

CONTROLS = ("number", "charge_discharge", "mode_power", "action", "modes")

#: Mode control: the modes tried, in order, for a behaviour without one.
_FALLBACK: dict[Behaviour, tuple[Behaviour, ...]] = {
    Behaviour.HOLD: (Behaviour.ABSORB_SURPLUS, Behaviour.CHARGE),
    Behaviour.DISCHARGE: (Behaviour.SELF_CONSUMPTION,),
    Behaviour.CHARGE: (Behaviour.ABSORB_SURPLUS,),
    Behaviour.ABSORB_SURPLUS: (Behaviour.SELF_CONSUMPTION,),
    Behaviour.SELF_CONSUMPTION: (),
}
_BEHAVIOURS = tuple(b.value for b in Behaviour if b is not Behaviour.STOP)


def _when(*controls: str) -> dict[str, Any]:
    return {"control": controls if len(controls) > 1 else controls[0]}


def _number(label: str, description: str, *controls: str, required: bool = True) -> ParamSpec:
    return ParamSpec(str, required=required, entity=True, domains=NUMBER_DOMAINS,
                     label=label, description=description, when=_when(*controls))


_MODE_ENTRY = ParamSpec(dict, fields={
    "option": ParamSpec(str, required=True, label="Option",
                        description="Option of the mode select"),
    "power": ParamSpec(str, entity=True, domains=NUMBER_DOMAINS, label="Power number",
                       description="Gets the commanded power (W or kW, without sign)"),
    "set": ParamSpec(dict, values=ParamSpec(object), label="Also set",
                     description="Fixed values written with the mode: {entity: value}"),
})

#: Keys of the ``power_setpoint`` binding (validation and config UI).
POWER_SETPOINT_BINDING: dict[str, ParamSpec] = {
    "control": ParamSpec(str, required=True, choices=CONTROLS, label="Controlled through",
                         description="number: one signed power · charge_discharge: two "
                                     "powers · mode_power: mode + power · action: a script · "
                                     "modes: the battery's own modes per behaviour"),
    "number": _number("Power number", "Signed power setpoint (+ charge, − discharge)",
                      "number"),
    "invert_sign": ParamSpec(bool, default=False, label="Invert sign", when=_when("number"),
                             description="The number takes discharging as positive"),
    "charge": _number("Charge power number", "Charge power (W or kW)", "charge_discharge"),
    "discharge": _number("Discharge power number", "Discharge power (W or kW)",
                         "charge_discharge"),
    "mode": ParamSpec(str, required=True, entity=True, domains=SELECT_DOMAINS,
                      label="Mode select", description="Select entity of the working mode",
                      when=_when("mode_power", "modes")),
    "power": _number("Power number", "Power for charging and discharging (no sign)",
                     "mode_power"),
    "charge_option": ParamSpec(str, required=True, label="Charge option",
                               when=_when("mode_power"), description="Forced charge mode"),
    "discharge_option": ParamSpec(str, required=True, label="Discharge option",
                                  when=_when("mode_power"),
                                  description="Forced discharge mode"),
    "hold_option": ParamSpec(str, required=True, label="Hold option", when=_when("mode_power"),
                             description="Mode that neither charges nor discharges"),
    "release_option": ParamSpec(str, label="Release option", when=_when("mode_power"),
                                description="Mode set when Energy Assistant stops (its own "
                                            "self-consumption)"),
    "action": ParamSpec(str, required=True, label="Action", when=_when("action"),
                        description="Script or action called with the power, e.g. "
                                    "script.set_battery_power"),
    "data": ParamSpec(dict, values=ParamSpec(object), advanced=True, label="Extra data",
                      when=_when("action"), description="More data for the action"),
    "power_key": ParamSpec(str, default="power_w", advanced=True, label="Power variable",
                           when=_when("action"),
                           description="Data key that gets the power in W"),
    "modes": ParamSpec(dict, required=True, label="Modes", when=_when("modes"),
                       values=_MODE_ENTRY,
                       description="Per behaviour (charge, discharge, hold, absorb_surplus, "
                                   "self_consumption): option, power, set"),
    "release": ParamSpec(str, label="On stop", choices=_BEHAVIOURS, when=_when("modes"),
                         description="Mode set when Energy Assistant stops (default "
                                     "self_consumption, if configured)"),
    "deadband_w": ParamSpec(float, default=25.0, minimum=0, advanced=True,
                            label="Deadband (W)",
                            description="Power changes up to this are not written"),
}


@dataclass
class _Mode:
    option: str | None
    power: NumberActuator | None = None
    fixed: list[tuple[Any, Any]] = field(default_factory=list)  # (actuator, value)


class BatteryControl:
    """Turns ``set_power_w`` commands (watt value + behaviour) into writes."""

    def __init__(
        self,
        where: str,
        control: str,
        *,
        numbers: dict[str, NumberActuator] | None = None,
        invert_sign: bool = False,
        mode: SelectActuator | None = None,
        modes: dict[Behaviour, _Mode] | None = None,
        release: Behaviour | str | None = None,
        action: ActionActuator | None = None,
    ) -> None:
        self.where = where
        self.control = control
        self._numbers = numbers or {}
        self._invert = invert_sign
        self._mode = mode
        self._modes = modes or {}
        self._release = release
        self._action = action
        self._warned: set[Behaviour] = set()

    @property
    def supported_behaviours(self) -> frozenset[Behaviour] | None:
        """Mode control: the behaviours with a mode.  Setpoint control: None."""
        return frozenset(self._modes) if self.control == "modes" else None

    @property
    def actuators(self) -> list[Any]:
        out: list[Any] = list(self._numbers.values())
        if self._mode is not None:
            out.append(self._mode)
        for m in self._modes.values():
            if m.power is not None:
                out.append(m.power)
            out.extend(a for a, _v in m.fixed)
        if self._action is not None:
            out.append(self._action)
        unique: dict[str, Any] = {}
        for a in out:
            unique.setdefault(a.entity_id, a)
        return list(unique.values())

    async def apply(self, power_w: float, behaviour: Behaviour | None) -> None:
        if self.control == "number":
            await self._numbers["number"].apply(-power_w if self._invert else power_w)
        elif self.control == "charge_discharge":
            await self._numbers["charge"].apply(max(0.0, power_w))
            await self._numbers["discharge"].apply(max(0.0, -power_w))
        elif self.control == "action":
            assert self._action is not None
            await self._action.apply(power_w)
        else:
            if self.control == "mode_power" or behaviour is None:
                behaviour = _by_sign(power_w)
            chosen = self._mode_for(behaviour)
            if chosen is None:
                if behaviour not in self._warned:
                    self._warned.add(behaviour)
                    _log.warning("%s: no mode for %s (or a fallback) — commands skipped",
                                 self.where, behaviour.value)
                return
            _used, mode = chosen
            # A hold that ended up in the charge mode charges at 0 W.
            await self._set_mode(mode, 0.0 if behaviour is Behaviour.HOLD else abs(power_w))

    def _mode_for(self, behaviour: Behaviour) -> tuple[Behaviour, _Mode] | None:
        for candidate in (behaviour, *_FALLBACK.get(behaviour, ())):
            mode = self._modes.get(candidate)
            if mode is not None:
                return candidate, mode
        return None

    async def _set_mode(self, mode: _Mode, power_w: float | None) -> None:
        # Values first, then the mode: the battery never starts a forced
        # mode with the previous power.
        if mode.power is not None and power_w is not None:
            await mode.power.apply(power_w)
        for actuator, value in mode.fixed:
            await actuator.apply(value)
        if mode.option is not None:
            assert self._mode is not None
            await self._mode.apply(mode.option)

    async def release(self) -> None:
        """Hand the battery back to its own operation (if a release mode is set)."""
        if self.control == "mode_power" and isinstance(self._release, str):
            assert self._mode is not None
            await self._mode.apply(self._release)
        elif self.control == "modes" and isinstance(self._release, Behaviour):
            mode = self._modes.get(self._release)
            if mode is not None:
                await self._set_mode(mode, None)


def _by_sign(power_w: float) -> Behaviour:
    if power_w > 0.5:
        return Behaviour.CHARGE
    return Behaviour.DISCHARGE if power_w < -0.5 else Behaviour.HOLD


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_control(where: str, client: Any, raw: Any) -> BatteryControl:
    """A ``BatteryControl`` from the ``power_setpoint`` binding."""
    if not isinstance(raw, dict):
        raise DeviceConfigError([f"{where}: must be a mapping with 'control: …'"])
    control = raw.get("control")
    if control not in CONTROLS:
        raise DeviceConfigError([(f"{where}: control must be one of {', '.join(CONTROLS)}, "
                                  f"got {control!r}")])
    deadband = float(raw.get("deadband_w", 25.0))

    def need(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise DeviceConfigError([f"{where}: control {control} needs '{key}'"])
        return value

    # One actuator per entity: two modes writing the same entity must share
    # its write state (rate limit, read-back), or one would hold back the
    # value the other just changed.
    actuators: dict[str, Any] = {}

    def number(entity: str) -> NumberActuator:
        if entity not in actuators:
            actuators[entity] = NumberActuator(where, client, entity, power=True,
                                               tolerance=deadband,
                                               minimum=0.0 if control != "number" else None)
        return actuators[entity]

    def select(entity: str) -> SelectActuator:
        if entity not in actuators:
            actuators[entity] = SelectActuator(where, client, entity)
        return actuators[entity]

    if control == "number":
        return BatteryControl(where, control, numbers={"number": NumberActuator(
            where, client, need("number"), power=True, tolerance=deadband)},
            invert_sign=bool(raw.get("invert_sign", False)))
    if control == "charge_discharge":
        return BatteryControl(where, control, numbers={
            "charge": number(need("charge")), "discharge": number(need("discharge"))})
    if control == "action":
        return BatteryControl(where, control, action=ActionActuator(
            where, client, need("action"), data=raw.get("data") or {},
            value_key=str(raw.get("power_key") or "power_w"), tolerance=deadband))
    mode_select = select(need("mode"))
    if control == "mode_power":
        power = number(need("power"))
        modes = {
            Behaviour.CHARGE: _Mode(need("charge_option"), power),
            Behaviour.DISCHARGE: _Mode(need("discharge_option"), power),
            Behaviour.HOLD: _Mode(need("hold_option")),
        }
        return BatteryControl(where, control, mode=mode_select, modes=modes,
                              release=raw.get("release_option") or None)
    modes_raw = raw.get("modes")
    if not isinstance(modes_raw, dict) or not modes_raw:
        raise DeviceConfigError([(f"{where}: control modes needs "
                                  "'modes: {<behaviour>: {option: …}}'")])
    errors: list[str] = []
    modes: dict[Behaviour, _Mode] = {}
    for name, entry in modes_raw.items():
        try:
            behaviour = Behaviour(name)
        except ValueError:
            errors.append(f"{where}: modes.{name}: not a behaviour ({', '.join(_BEHAVIOURS)})")
            continue
        if behaviour is Behaviour.STOP or not isinstance(entry, dict) or not entry.get("option"):
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
        power = entry.get("power")
        modes[behaviour] = _Mode(str(entry["option"]),
                                 number(str(power)) if power else None, fixed)
    release_raw = raw.get("release")
    release = Behaviour(release_raw) if release_raw else (
        Behaviour.SELF_CONSUMPTION if Behaviour.SELF_CONSUMPTION in modes else None)
    if release is not None and release not in modes:
        errors.append(f"{where}: release {release.value!r} has no mode in 'modes'")
    if errors:
        raise DeviceConfigError(errors)
    return BatteryControl(where, control, mode=mode_select, modes=modes, release=release)

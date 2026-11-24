"""Control strategies — how a ``DeviceCommand`` becomes Modbus register writes.

Reading a device is plain data (see ``profile.py``).  Commanding one is not:
vendors differ in *logic* — a percentage limit here, a mode register plus a
watt setpoint there, a keep-alive somewhere else.  So a profile does not
describe control as a mapping; it selects one of the strategies implemented
here and tells it which registers to use::

    control:
      strategy: discharge_limit_pct
      register: active_power_limit

A strategy is a pure translator: ``plan(command)`` returns the register
values to write and performs no I/O.  ``ModbusDevice`` encodes and sends
them.  Adding a vendor with a new control scheme means adding one class here
and registering it in ``STRATEGIES``.
"""

from __future__ import annotations

from typing import Any, Protocol

from ...core.models import DeviceCommand


class ControlConfigError(ValueError):
    """The device config lacks something the selected strategy needs."""


class ControlStrategy(Protocol):
    #: False when the hardware offers no way to command charge power.
    controls_charge: bool

    def plan(self, command: DeviceCommand) -> list[tuple[str, float]]:
        """Translate *command* into ``(register name, value)`` writes."""
        ...


# Optional strategy attributes (read with getattr by ``ModbusDevice``):
#
# * ``refresh_s: float`` — write a command only when its register values
#   change, and repeat unchanged values at most this often.  Without it every
#   command is written.
# * ``release() -> list[tuple[str, float]]`` — writes that hand the device
#   back to its own operation; sent when Energy Assistant starts, reloads and
#   stops.


class DischargeLimitPct:
    """Cap discharge power through one "percent of rated power" register.

    Used by the SMA Sunny Boy Storage, whose only cyclic control point is the
    active power limit (register 40016): X % lets the inverter feed out up to
    X % of its rated power, 0 % stops discharging.  Charging cannot be
    commanded — the inverter charges on its own from surplus.

    ``set_power_w`` (platform sign: negative = discharge) becomes::

        pct = |power_w| / (max_discharge_w × voltage_max_v / voltage_nominal_v) × 100

    clamped to 0–100 and rounded.  A charge or idle request writes 0 %.

    The voltage factor (default 253 V / 230 V) accounts for the inverter's
    rated power being specified at the upper grid-voltage limit.

    Device config keys: ``max_discharge_kw`` (required), ``voltage_max_v``,
    ``voltage_nominal_v``.
    """

    controls_charge = False

    def __init__(self, registers: dict[str, str], device_cfg: dict[str, Any]) -> None:
        self._register = registers["register"]
        max_discharge_w = float(device_cfg.get("max_discharge_kw") or 0.0) * 1000.0
        if max_discharge_w <= 0:
            raise ControlConfigError("'max_discharge_kw' must be set and positive")
        v_factor = float(device_cfg.get("voltage_max_v", 253.0)) / float(
            device_cfg.get("voltage_nominal_v", 230.0)
        )
        self._full_scale_w = max_discharge_w * v_factor

    def plan(self, command: DeviceCommand) -> list[tuple[str, float]]:
        if command.command != "set_power_w":
            raise NotImplementedError(
                f"discharge_limit_pct does not support command {command.command!r}"
            )
        power_w = float(command.value)
        if power_w >= 0:
            pct = 0
        else:
            pct = int(round(min(100.0, max(0.0, -power_w / self._full_scale_w * 100.0))))
        return [(self._register, pct)]


class BmsOperatingMode:
    """Command charge and discharge power through the battery management
    (``CmpBMS``) registers of the SMA Sunny Boy Storage.

    The registers (all U32 holding, write-only) and their use follow the
    evcc template for the SBS 3.7/5.0/6.0 and were verified on an SBS 3.7
    (2026-11-24: a 500 W charge command was followed within 2 s, from the
    grid as well):

    ========== =================================== ==========================
    role       register                            values
    ========== =================================== ==========================
    mode       40236 CmpBMS.OpMod                  2424 default, 2289 charge
    charge_min 40793 CmpBMS.BatChaMinW             W
    charge_max 40795 CmpBMS.BatChaMaxW             W
    dsch_min   40797 CmpBMS.BatDschMinW            W
    dsch_max   40799 CmpBMS.BatDschMaxW            W
    grid       40801 CmpBMS.GridWSpt               W (target at its meter)
    power_limit 40016 active power limit           % (kept at 100)
    ========== =================================== ==========================

    ``set_power_w`` (platform sign: positive = charge), rounded to
    ``bms_step_w`` (default 50 W) so small changes of a tracking setpoint do
    not cause writes:

    * ``> 0`` — charge mode with charge min = max = P: exactly P, from PV or
      the grid.
    * ``< 0`` — default mode, no charging, discharge up to |P|: the inverter
      covers the load at its own meter up to that cap.
    * ``0`` — default mode with charge and discharge max 0: hold.

    ``release()`` — default mode with the full limits: the inverter's own
    self-consumption, as without Energy Assistant.  The inverter does **not**
    fall back to it on its own when commands stop (verified: a charge command
    held for minutes), so it is sent at every start, reload and stop.

    Unchanged commands are repeated every ``refresh_s`` only — the values
    are parameters, not a cyclic setpoint.

    Device config keys: ``max_charge_kw`` and ``max_discharge_kw`` (required),
    ``bms_step_w``.
    """

    controls_charge = True
    refresh_s = 300.0

    MODE_DEFAULT = 2424
    MODE_CHARGE = 2289

    def __init__(self, registers: dict[str, str], device_cfg: dict[str, Any]) -> None:
        self._r = registers
        self._max_charge_w = float(device_cfg.get("max_charge_kw") or 0.0) * 1000.0
        self._max_discharge_w = float(device_cfg.get("max_discharge_kw") or 0.0) * 1000.0
        if self._max_charge_w <= 0 or self._max_discharge_w <= 0:
            raise ControlConfigError("'max_charge_kw' and 'max_discharge_kw' must be set and positive")
        self._step_w = float(device_cfg.get("bms_step_w", 50.0))
        if self._step_w <= 0:
            raise ControlConfigError("'bms_step_w' must be positive")

    def _writes(
        self, mode: int, cha_min: float, cha_max: float, dsch_min: float, dsch_max: float
    ) -> list[tuple[str, float]]:
        r = self._r
        return [
            (r["mode"], mode),
            (r["charge_min"], cha_min),
            (r["charge_max"], cha_max),
            (r["discharge_min"], dsch_min),
            (r["discharge_max"], dsch_max),
            (r["grid_setpoint"], 0),
            (r["power_limit"], 100),
        ]

    def plan(self, command: DeviceCommand) -> list[tuple[str, float]]:
        if command.command != "set_power_w":
            raise NotImplementedError(
                f"bms_operating_mode does not support command {command.command!r}"
            )
        power_w = round(float(command.value) / self._step_w) * self._step_w
        if power_w > 0:
            p = min(power_w, self._max_charge_w)
            return self._writes(self.MODE_CHARGE, p, p, 0, 0)
        if power_w < 0:
            return self._writes(self.MODE_DEFAULT, 0, 0, 0, min(-power_w, self._max_discharge_w))
        return self._writes(self.MODE_DEFAULT, 0, 0, 0, 0)

    def release(self) -> list[tuple[str, float]]:
        return self._writes(
            self.MODE_DEFAULT, 0, self._max_charge_w, 0, self._max_discharge_w
        )


# strategy name -> (class, register roles the profile must supply)
STRATEGIES: dict[str, tuple[type, tuple[str, ...]]] = {
    "discharge_limit_pct": (DischargeLimitPct, ("register",)),
    "bms_operating_mode": (
        BmsOperatingMode,
        ("mode", "charge_min", "charge_max", "discharge_min", "discharge_max",
         "grid_setpoint", "power_limit"),
    ),
}


def build_strategy(
    name: str, registers: dict[str, str], device_cfg: dict[str, Any]
) -> ControlStrategy:
    """Instantiate strategy *name* for one device."""
    cls, _roles = STRATEGIES[name]
    return cls(registers, device_cfg)

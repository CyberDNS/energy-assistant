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


# strategy name -> (class, register roles the profile must supply)
STRATEGIES: dict[str, tuple[type, tuple[str, ...]]] = {
    "discharge_limit_pct": (DischargeLimitPct, ("register",)),
}


def build_strategy(
    name: str, registers: dict[str, str], device_cfg: dict[str, Any]
) -> ControlStrategy:
    """Instantiate strategy *name* for one device."""
    cls, _roles = STRATEGIES[name]
    return cls(registers, device_cfg)

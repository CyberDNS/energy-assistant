"""Modbus device profiles — the vendor-specific register mapping as data.

A profile describes *one device model*: which registers exist, how to decode
them, and which of them feed the platform's ``DeviceState``.  Everything that
describes *one installation* (host, capacity, SoC limits, …) stays in the
device entry of ``config.yaml``.

Profile file format (YAML)::

    name: "SMA Sunny Boy Storage"

    defaults:            # connection defaults, overridable per device
      port: 502
      unit_id: 3
      word_order: big    # big | little — order of 16-bit words in wider values

    nan:                 # raw values the device sends for "no value", per type
      s32: 0x80000000
      u32: 0xFFFFFFFF

    registers:           # every register the profile knows, by a free name
      power_ac:
        table: input     # input | holding
        address: 30775   # address as sent on the wire
        type: s32        # s16 u16 s32 u32 s64 u64 f32 f64
        scale: 1         # optional: decoded value × scale (fixed-point factors)

    state:               # which registers fill DeviceState
      power_w: {register: power_ac, scale: -1}   # scale: sign / unit conversion
      soc_pct: soc                               # short form: register name only
      extra:
        max_charge_w: max_charge

    control:             # optional — omit for read-only devices
      strategy: discharge_limit_pct     # a strategy from control.py
      register: active_power_limit      # register role(s) the strategy needs

Only registers referenced under ``state`` are polled.  Registers used for
control must be holding registers; they are written, never polled.

Bundled profiles live next to this module in ``profiles/``; a device selects
one by file stem (``profile: sma_sunny_boy_storage``) or points at its own
file (``profile: /config/my_battery.yaml``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .._modbus import codec
from .._modbus.client import TABLE_HOLDING, TABLES
from .control import STRATEGIES

PROFILE_DIR = Path(__file__).parent / "profiles"

# DeviceState fields a profile may map a register to.
STATE_FIELDS = ("power_w", "soc_pct", "energy_kwh")


class ProfileError(ValueError):
    """The profile file is missing or malformed."""


@dataclass(frozen=True)
class RegisterDef:
    name: str
    table: str
    address: int
    type: str
    scale: float = 1.0

    @property
    def count(self) -> int:
        return codec.word_count(self.type)


@dataclass(frozen=True)
class StateSource:
    """One ``DeviceState`` value: a register and an optional conversion factor."""

    register: str
    scale: float = 1.0


@dataclass(frozen=True)
class ControlDef:
    """Selected control strategy and the registers it writes, by role."""

    strategy: str
    registers: dict[str, str]


@dataclass(frozen=True)
class ModbusProfile:
    name: str
    registers: dict[str, RegisterDef]
    state: dict[str, StateSource] = field(default_factory=dict)
    extra: dict[str, StateSource] = field(default_factory=dict)
    nan: dict[str, int] = field(default_factory=dict)
    control: ControlDef | None = None
    port: int = 502
    unit_id: int = 1
    word_order: codec.WordOrder = "big"

    def polled_registers(self) -> list[RegisterDef]:
        """Registers referenced by ``state`` / ``extra`` — the ones worth reading."""
        names = {s.register for s in (*self.state.values(), *self.extra.values())}
        return [self.registers[name] for name in sorted(names)]


def available_profiles() -> list[str]:
    """Names of the bundled profiles."""
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))


def load_profile(ref: str | Path) -> ModbusProfile:
    """Load a profile by bundled name or by file path."""
    ref_str = str(ref)
    is_path = "/" in ref_str or ref_str.endswith((".yaml", ".yml"))
    path = Path(ref_str) if is_path else PROFILE_DIR / f"{ref_str}.yaml"
    if not path.is_file():
        raise ProfileError(
            f"Modbus profile {ref_str!r} not found ({path}). "
            f"Bundled profiles: {', '.join(available_profiles()) or 'none'}"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return parse_profile(raw, default_name=path.stem)
    except ProfileError as exc:
        raise ProfileError(f"{path}: {exc}") from exc


def parse_profile(raw: Any, default_name: str = "unnamed") -> ModbusProfile:
    """Validate a parsed profile document and build a ``ModbusProfile``.

    Unknown keys are rejected rather than ignored: a typo in a register
    mapping should fail at start-up, not surface as a silently missing value.
    """
    if not isinstance(raw, dict):
        raise ProfileError("profile must be a mapping")
    _check_keys(raw, {"name", "defaults", "nan", "registers", "state", "control"}, "profile")

    defaults = raw.get("defaults") or {}
    _check_keys(defaults, {"port", "unit_id", "word_order"}, "defaults")
    word_order = defaults.get("word_order", "big")
    if word_order not in ("big", "little"):
        raise ProfileError(f"defaults.word_order must be big or little, got {word_order!r}")

    nan: dict[str, int] = {}
    for type_name, value in (raw.get("nan") or {}).items():
        _check_type(type_name, f"nan.{type_name}")
        nan[type_name] = _as_int(value, f"nan.{type_name}")

    registers: dict[str, RegisterDef] = {}
    raw_registers = raw.get("registers")
    if not isinstance(raw_registers, dict) or not raw_registers:
        raise ProfileError("profile needs a non-empty 'registers' mapping")
    for name, cfg in raw_registers.items():
        where = f"registers.{name}"
        if not isinstance(cfg, dict):
            raise ProfileError(f"{where} must be a mapping")
        _check_keys(cfg, {"table", "address", "type", "scale"}, where)
        for key in ("table", "address", "type"):
            if key not in cfg:
                raise ProfileError(f"{where} is missing '{key}'")
        if cfg["table"] not in TABLES:
            raise ProfileError(f"{where}.table must be one of {', '.join(TABLES)}")
        _check_type(cfg["type"], f"{where}.type")
        registers[name] = RegisterDef(
            name=name,
            table=cfg["table"],
            address=_as_int(cfg["address"], f"{where}.address"),
            type=cfg["type"],
            scale=_as_float(cfg.get("scale", 1.0), f"{where}.scale"),
        )

    raw_state = dict(raw.get("state") or {})
    raw_extra = raw_state.pop("extra", None) or {}
    _check_keys(raw_state, set(STATE_FIELDS), "state")
    state = {
        key: _state_source(value, f"state.{key}", registers) for key, value in raw_state.items()
    }
    extra = {
        key: _state_source(value, f"state.extra.{key}", registers)
        for key, value in raw_extra.items()
    }

    control: ControlDef | None = None
    raw_control = raw.get("control")
    if raw_control is not None:
        if not isinstance(raw_control, dict) or "strategy" not in raw_control:
            raise ProfileError("control must be a mapping with a 'strategy'")
        strategy = raw_control["strategy"]
        if strategy not in STRATEGIES:
            raise ProfileError(
                f"control.strategy {strategy!r} is unknown; known: {', '.join(sorted(STRATEGIES))}"
            )
        roles = STRATEGIES[strategy][1]
        _check_keys(raw_control, {"strategy", *roles}, "control")
        control_registers: dict[str, str] = {}
        for role in roles:
            reg_name = raw_control.get(role)
            if reg_name not in registers:
                raise ProfileError(f"control.{role} must name a declared register")
            if registers[reg_name].table != TABLE_HOLDING:
                raise ProfileError(f"control.{role}: {reg_name!r} is not a holding register")
            control_registers[role] = reg_name
        control = ControlDef(strategy=strategy, registers=control_registers)

    return ModbusProfile(
        name=str(raw.get("name") or default_name),
        registers=registers,
        state=state,
        extra=extra,
        nan=nan,
        control=control,
        port=_as_int(defaults.get("port", 502), "defaults.port"),
        unit_id=_as_int(defaults.get("unit_id", 1), "defaults.unit_id"),
        word_order=word_order,
    )


def _state_source(value: Any, where: str, registers: dict[str, RegisterDef]) -> StateSource:
    if isinstance(value, str):
        value = {"register": value}
    if not isinstance(value, dict) or "register" not in value:
        raise ProfileError(f"{where} must be a register name or {{register: ..., scale: ...}}")
    _check_keys(value, {"register", "scale"}, where)
    if value["register"] not in registers:
        raise ProfileError(f"{where} refers to unknown register {value['register']!r}")
    return StateSource(
        register=value["register"],
        scale=_as_float(value.get("scale", 1.0), f"{where}.scale"),
    )


def _check_keys(cfg: Any, allowed: set[str], where: str) -> None:
    if not isinstance(cfg, dict):
        raise ProfileError(f"{where} must be a mapping")
    unknown = set(cfg) - allowed
    if unknown:
        raise ProfileError(
            f"{where} has unknown key(s) {sorted(unknown)}; allowed: {sorted(allowed)}"
        )


def _check_type(type_name: Any, where: str) -> None:
    if not isinstance(type_name, str) or not codec.is_known_type(type_name):
        raise ProfileError(
            f"{where}: unknown type {type_name!r}; known: {', '.join(codec.known_types())}"
        )


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"{where} must be an integer, got {value!r}")
    return value


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where} must be a number, got {value!r}")
    return float(value)

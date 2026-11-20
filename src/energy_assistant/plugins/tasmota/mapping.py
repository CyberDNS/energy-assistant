"""Map a Tasmota ``Status 10`` payload to a :class:`TasmotaReading`.

``GET /cm?cmnd=Status 10`` answers with the sensor JSON of the device::

    {"StatusSNS": {"Time": "2026-11-19T20:51:11",
                   "MT631": {"E_in": 18940.697, "E_out": 2485.291, "P": 770,
                             "L1": 303, "L2": 113, "L3": 352}}}

Below ``StatusSNS`` there is one object per sensor: ``ENERGY`` on a smart plug,
or the name a smart-meter (SML) script gives its meter (``MT631`` above, from
the script line ``+1,3,s,0,9600,MT631``).  The value names inside come from
the device too.

A :class:`FieldMap` names the values to read as dotted paths.  With a
``sensor`` the paths are relative to that sensor object (``P``); without one
they start at ``StatusSNS`` (``MT631.P``).  A list element is addressed by its
index (``Power.1``).  When the map came from a profile (``auto_sensor``) and
the payload has no sensor of that name — the script calls its meter
differently — the one sensor object holding the power value is used instead.

``Time`` is the device's local time without a UTC offset and is ignored;
readings are stamped with the UTC receive time.

A payload without a usable power value raises :class:`PayloadError`; a
missing or non-numeric optional field is left out of the reading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Field names of a FieldMap: scalar fields and the per-phase list fields.
SCALAR_FIELDS = ("power_w", "energy_in_kwh", "energy_out_kwh", "voltage_v", "current_a")
PHASE_FIELDS = {"phase_power_w": "power_w", "phase_voltage_v": "voltage_v", "phase_current_a": "current_a"}

_POWER_KEYS = ("P", "Power", "power", "P_total", "Power_curr", "Power_cur", "power_curr")


class PayloadError(ValueError):
    """The payload is not usable: no ``StatusSNS`` or no valid power value."""


class MappingError(ValueError):
    """The field configuration is invalid."""


@dataclass(frozen=True)
class FieldSpec:
    """One value to read: a dotted path and a scale factor."""

    path: tuple[str, ...]
    scale: float = 1.0

    @property
    def label(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True)
class FieldMap:
    """Which values of the payload feed which reading fields."""

    scalars: dict[str, FieldSpec] = field(default_factory=dict)
    phases: dict[str, tuple[FieldSpec, ...]] = field(default_factory=dict)
    extra: dict[str, FieldSpec] = field(default_factory=dict)
    invert_sign: bool = False
    sensor: str | None = None
    """Sensor object below ``StatusSNS`` the paths are relative to (None: paths start at ``StatusSNS``)."""
    auto_sensor: bool = False
    """Fall back to the one sensor object holding the power value when ``sensor`` is not in the payload."""

    @property
    def power(self) -> FieldSpec:
        return self.scalars["power_w"]

    def specs(self) -> list[FieldSpec]:
        return [*self.scalars.values(), *(s for ss in self.phases.values() for s in ss), *self.extra.values()]


@dataclass(frozen=True)
class TasmotaReading:
    """One mapped ``Status 10`` payload, stamped at receive time (UTC)."""

    timestamp: datetime
    power_w: float
    """Positive = consuming / importing, negative = producing / exporting."""
    energy_in_kwh: float | None = None
    energy_out_kwh: float | None = None
    values: dict[str, float] = field(default_factory=dict)
    """Further mapped values: ``voltage_v``, ``current_a``, ``l1_power_w`` …, extra fields."""
    sensor: str | None = None
    """The sensor object the values were read from."""


# ---------------------------------------------------------------------------
# configuration


def build_field_map(
    fields: Any = None,
    *,
    base: dict[str, Any] | None = None,
    sensor: str | None = None,
    auto_sensor: bool = False,
    invert_sign: bool = False,
) -> FieldMap:
    """Build a :class:`FieldMap` from ``base`` fields (a profile's) and ``fields`` overrides.

    A field is a path string, or a dict ``{path: ..., scale: ...}``;
    ``phase_*`` fields take a list of those.  ``extra`` maps further names to
    paths.  A ``fields`` entry replaces the base entry of the same name;
    ``None`` removes it (``extra`` entries are merged one by one).
    """
    raw: dict[str, Any] = dict(base or {})
    if fields is not None:
        if not isinstance(fields, dict):
            raise MappingError("'fields' must be a mapping")
        for name, value in fields.items():
            if name == "extra" and isinstance(value, dict) and isinstance(raw.get("extra"), dict):
                raw["extra"] = {**raw["extra"], **value}
            else:
                raw[name] = value

    scalars: dict[str, FieldSpec] = {}
    phases: dict[str, tuple[FieldSpec, ...]] = {}
    extra: dict[str, FieldSpec] = {}
    for name, value in raw.items():
        if value is None:
            continue  # lets a config switch off a profile field
        if name in SCALAR_FIELDS:
            scalars[name] = _spec(value, name)
        elif name in PHASE_FIELDS:
            if not isinstance(value, (list, tuple)) or not value:
                raise MappingError(f"field {name!r} must be a list of paths (L1, L2, L3)")
            phases[name] = tuple(_spec(v, name) for v in value)
        elif name == "extra":
            if not isinstance(value, dict):
                raise MappingError("'fields.extra' must map names to paths")
            extra = {str(k): _spec(v, f"extra.{k}") for k, v in value.items() if v is not None}
        else:
            known = ", ".join((*SCALAR_FIELDS, *PHASE_FIELDS, "extra"))
            raise MappingError(f"unknown field {name!r} (known: {known})")
    if "power_w" not in scalars:
        raise MappingError("no 'power_w' path — set fields.power_w or choose a profile")
    sensor = str(sensor).strip() if sensor else None
    return FieldMap(
        scalars=scalars, phases=phases, extra=extra, invert_sign=invert_sign,
        sensor=sensor or None, auto_sensor=auto_sensor and bool(sensor),
    )


def _spec(value: Any, name: str) -> FieldSpec:
    scale: Any = 1.0
    if isinstance(value, dict):
        scale = value.get("scale", 1.0)
        value = value.get("path")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # YAML reads an unquoted 1_8_0 as the number 1670.
        raise MappingError(f"field {name!r}: path {value!r} is a number — quote it (\"1_8_0\")")
    path = str(value if value is not None else "").strip()
    if not path:
        raise MappingError(f"field {name!r} has no path")
    try:
        scale = float(scale)
    except (TypeError, ValueError) as exc:
        raise MappingError(f"field {name!r}: invalid scale {scale!r}") from exc
    if not math.isfinite(scale) or scale == 0:
        raise MappingError(f"field {name!r}: scale must be a non-zero number")
    return FieldSpec(path=tuple(p for p in path.split(".") if p), scale=scale)


# ---------------------------------------------------------------------------
# payload


def sensors(payload: Any) -> dict[str, Any]:
    """The ``StatusSNS`` object of a ``Status 10`` answer."""
    if not isinstance(payload, dict):
        raise PayloadError("payload is not a JSON object")
    if "StatusSNS" in payload:
        sns = payload["StatusSNS"]
        if not isinstance(sns, dict):
            raise PayloadError("StatusSNS is not an object")
        return sns
    if "WARNING" in payload:
        raise PayloadError(f"device answered: {payload['WARNING']}")
    if "Command" in payload:  # {"Command":"Unknown"}
        raise PayloadError(f"device answered: Command {payload['Command']}")
    raise PayloadError("payload has no StatusSNS")


def select_sensor(sns: dict[str, Any], fields: FieldMap) -> tuple[dict[str, Any], str | None]:
    """The object the field paths start at, and the sensor's name."""
    if fields.sensor is None:
        return sns, None
    obj = sns.get(fields.sensor)
    if isinstance(obj, dict):
        return obj, fields.sensor
    if fields.auto_sensor:
        found = [
            name for name, o in sns.items()
            if isinstance(o, dict) and resolve(o, fields.power, summable=True) is not None
        ]
        if len(found) == 1:
            return sns[found[0]], found[0]
        if found:
            raise PayloadError(
                f"no sensor {fields.sensor!r}; {', '.join(found)} all have "
                f"{fields.power.label!r} — set 'sensor' to pick one"
            )
    raise PayloadError(f"no sensor {fields.sensor!r} in the payload")


def resolve(root: dict[str, Any], spec: FieldSpec, *, summable: bool = False) -> float | None:
    """The numeric value at ``spec.path``, scaled; None when missing or not numeric."""
    node: Any = root
    for key in spec.path:
        if isinstance(node, dict):
            if key not in node:
                return None
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    if isinstance(node, list) and summable:
        nums = [_num(v) for v in node]
        if not nums or any(v is None for v in nums):
            return None
        value: float | None = sum(nums)  # type: ignore[arg-type]
    else:
        value = _num(node)
    return None if value is None else value * spec.scale


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def map_payload(payload: Any, fields: FieldMap, *, received_at: datetime | None = None) -> TasmotaReading:
    """Map a decoded ``Status 10`` payload; raise :class:`PayloadError` without valid power."""
    root, sensor = select_sensor(sensors(payload), fields)
    sign = -1.0 if fields.invert_sign else 1.0

    power = resolve(root, fields.power, summable=True)
    if power is None:
        where = f"{sensor}.{fields.power.label}" if sensor else fields.power.label
        raise PayloadError(f"no numeric value at {where}")

    values: dict[str, float] = {}
    for name in ("voltage_v", "current_a"):
        spec = fields.scalars.get(name)
        v = resolve(root, spec) if spec else None
        if v is not None:
            values[name] = v
    for name, specs in fields.phases.items():
        suffix = PHASE_FIELDS[name]
        for i, spec in enumerate(specs, start=1):
            v = resolve(root, spec)
            if v is not None:
                values[f"l{i}_{suffix}"] = v * sign if suffix == "power_w" else v
    for name, spec in fields.extra.items():
        v = resolve(root, spec)
        if v is not None:
            values[name] = v

    def counter(name: str) -> float | None:
        spec = fields.scalars.get(name)
        return resolve(root, spec, summable=True) if spec else None

    return TasmotaReading(
        timestamp=received_at or datetime.now(UTC),
        power_w=power * sign,
        energy_in_kwh=counter("energy_in_kwh"),
        energy_out_kwh=counter("energy_out_kwh"),
        values=values,
        sensor=sensor,
    )


def suggest_power_paths(payload: Any) -> list[str]:
    """Paths that look like a power value (``<Sensor>.P`` / ``<Sensor>.Power``), for the log."""
    try:
        sns = sensors(payload)
    except PayloadError:
        return []
    found: list[str] = []
    for name, obj in sns.items():
        if not isinstance(obj, dict):
            continue
        for key in _POWER_KEYS:
            if key in obj and (_num(obj[key]) is not None or isinstance(obj[key], list)):
                found.append(f"{name}.{key}")
                break
    return found

"""Read bindings: a capability's value from Home Assistant entities.

Every Home Assistant device type binds its capabilities the same way::

    capabilities:
      power_w:       { entity: sensor.grid_power, invert_sign: true }
      power_w:       { import_entity: sensor.grid_import, export_entity: sensor.grid_export }
      energy_in_kwh: { entity: sensor.grid_energy_import }
      phase_power_w: { entities: [sensor.l1_power, sensor.l2_power, sensor.l3_power] }
      temperature_c: { entity: climate.living_room, attribute: current_temperature }
      soc_pct:       { entity: sensor.battery_soc, scale: 0.1 }
      plugged:       { entity: sensor.wallbox_status, on: [Charging, "Wait car", Complete] }

Keys
----
``entity``          the entity whose state is the value
``attribute``       read this attribute of ``entity`` instead of its state
``import_entity`` / ``export_entity``
                    power and energy: two entities, value = import − export
``entities``        per-phase power: one entity per phase
``invert_sign``     power: the entity counts the other direction as positive
``scale``           multiply the value (after unit conversion)
``on``              on/off values: the states that count as on (default:
                    ``on``, ``true``, ``home``, ``connected``, ``plugged``, …)

Units
-----
Numbers are converted from the entity's ``unit_of_measurement`` to the
capability's unit: W/kW/MW → W, Wh/kWh/MWh → kWh, °F/K → °C, m/mi → km.
An entity without a unit (or an attribute) is taken as already in it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ...core.device_model import Capability, DeviceConfigError, ParamSpec
from .client_types import EntityState

_log = logging.getLogger(__name__)

_GONE = frozenset({"unavailable", "unknown", "none", ""})

#: States that count as "on" when a binding does not set ``on``.
DEFAULT_ON = frozenset({
    "on", "true", "1", "yes", "home", "connected", "plugged", "plugged_in", "open",
})


class ValueKind(str, Enum):
    """What a capability's value is, and which unit it is converted to."""

    POWER = "power"              # W
    ENERGY = "energy"            # kWh
    PERCENT = "percent"          # %
    DISTANCE = "distance"        # km
    TEMPERATURE = "temperature"  # °C
    BOOL = "bool"


#: Unit of measurement → factor to the kind's unit (lower case).
_FACTORS: dict[ValueKind, dict[str, float]] = {
    ValueKind.POWER: {"w": 1.0, "kw": 1000.0, "mw": 1e6, "gw": 1e9},
    ValueKind.ENERGY: {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0, "gwh": 1e6},
    ValueKind.DISTANCE: {"km": 1.0, "m": 0.001, "mi": 1.609344, "ft": 0.0003048},
    ValueKind.PERCENT: {"%": 1.0},
}

CAPABILITY_KINDS: dict[Capability, ValueKind] = {
    Capability.POWER_W: ValueKind.POWER,
    Capability.ENERGY_IN_KWH: ValueKind.ENERGY,
    Capability.ENERGY_OUT_KWH: ValueKind.ENERGY,
    Capability.PHASE_POWER_W: ValueKind.POWER,
    Capability.SOC_PCT: ValueKind.PERCENT,
    Capability.RANGE_KM: ValueKind.DISTANCE,
    Capability.TEMPERATURE_C: ValueKind.TEMPERATURE,
    Capability.PLUGGED: ValueKind.BOOL,
    Capability.LOCATION_HOME: ValueKind.BOOL,
    Capability.SWITCH: ValueKind.BOOL,
}

#: Entity domains whose state (or attribute) can be a number.
NUMERIC_DOMAINS = (
    "sensor", "number", "input_number", "climate", "water_heater", "weather",
    "counter", "utility_meter",
)
#: Entity domains whose state can be on/off.
BOOL_DOMAINS = (
    "binary_sensor", "sensor", "switch", "input_boolean", "light", "fan",
    "device_tracker", "person", "input_select", "select", "climate", "cover", "lock",
)


def _convert(value: float, kind: ValueKind, unit: str | None) -> float:
    if not unit:
        return value
    u = unit.strip().lower()
    if kind is ValueKind.TEMPERATURE:
        if u in ("°f", "f", "fahrenheit"):
            return (value - 32.0) * 5.0 / 9.0
        if u in ("k", "kelvin"):
            return value - 273.15
        return value
    factors = _FACTORS.get(kind) or {}
    return value * factors.get(u, 1.0)


def _float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


@dataclass(frozen=True)
class Reading:
    """The value of one binding at one moment."""

    value: Any
    """float (or list of floats for ``entities``), bool, or None when unavailable."""
    reported_at: datetime | None = None
    """When Home Assistant last updated the (oldest) entity."""
    raw: str | None = None
    """The raw state (single-entity bindings)."""

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass
class ReadBinding:
    """A capability bound to one or more Home Assistant entities."""

    where: str
    """``device 'x': capabilities.power_w`` — for log messages."""
    kind: ValueKind
    entity: str | None = None
    attribute: str | None = None
    import_entity: str | None = None
    export_entity: str | None = None
    entities: tuple[str, ...] = ()
    invert_sign: bool = False
    scale: float = 1.0
    on: frozenset[str] = DEFAULT_ON
    _missing: set[str] = field(default_factory=set, repr=False)

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Every entity this binding reads."""
        if self.entities:
            return self.entities
        if self.import_entity:
            return (self.import_entity, str(self.export_entity))
        return (str(self.entity),)

    async def _get(self, client: Any, entity_id: str) -> EntityState | None:
        try:
            state = await client.get_entity(entity_id)
        except Exception as exc:  # noqa: BLE001 — one bad entity must not hide the others
            _log.debug("%s: reading %s failed: %s", self.where, entity_id, exc)
            return None
        if state is None:
            if entity_id not in self._missing:
                self._missing.add(entity_id)
                _log.warning("%s: entity %s not found in Home Assistant", self.where, entity_id)
            return None
        self._missing.discard(entity_id)
        return state

    def _number(self, state: EntityState | None, *, attribute: str | None = None) -> float | None:
        if state is None:
            return None
        if attribute:
            return _float(state.attributes.get(attribute))
        if state.state is None or state.state.lower() in _GONE:
            return None
        value = _float(state.state)
        if value is None:
            return None
        return _convert(value, self.kind, state.attributes.get("unit_of_measurement"))

    def _bool(self, state: EntityState | None) -> bool | None:
        if state is None:
            return None
        raw = state.attributes.get(self.attribute) if self.attribute else state.state
        if raw is None:
            return None
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if not self.attribute and text in _GONE:
            return None
        return text in self.on

    async def read(self, client: Any) -> Reading:
        """Read the binding's current value (``value`` None when unavailable)."""
        if self.kind is ValueKind.BOOL:
            state = await self._get(client, str(self.entity))
            return Reading(self._bool(state), _ts(state), _raw(state))
        if self.entities:
            states = [await self._get(client, e) for e in self.entities]
            values = [self._number(s) for s in states]
            if any(v is None for v in values):
                return Reading(None)
            return Reading([self._finish(v) for v in values], _oldest(states))  # type: ignore[arg-type]
        if self.import_entity:
            imp = await self._get(client, self.import_entity)
            exp = await self._get(client, str(self.export_entity))
            a, b = self._number(imp), self._number(exp)
            if a is None or b is None:
                return Reading(None)
            return Reading(self._finish(a - b), _oldest([imp, exp]))
        state = await self._get(client, str(self.entity))
        value = self._number(state, attribute=self.attribute)
        return Reading(None if value is None else self._finish(value), _ts(state), _raw(state))

    async def read_pair(self, client: Any) -> tuple[float | None, float | None]:
        """``(import, export)`` of an import/export binding (converted, scaled)."""
        if not self.import_entity:
            return (None, None)
        imp = self._number(await self._get(client, self.import_entity))
        exp = self._number(await self._get(client, str(self.export_entity)))
        return (None if imp is None else imp * self.scale,
                None if exp is None else exp * self.scale)

    def _finish(self, value: float) -> float:
        value *= self.scale
        return -value if self.invert_sign else value


def _ts(state: EntityState | None) -> datetime | None:
    return state.last_reported if state is not None else None


def _raw(state: EntityState | None) -> str | None:
    return state.state if state is not None else None


def _oldest(states: list[EntityState | None]) -> datetime | None:
    stamps = [s.last_reported for s in states if s is not None and s.last_reported]
    return min(stamps) if stamps else None


# ---------------------------------------------------------------------------
# Binding specs (validation and config UI)
# ---------------------------------------------------------------------------

#: Units of a fitting entity per value kind (config UI suggestions).
UNITS: dict[ValueKind, tuple[str, ...]] = {
    ValueKind.POWER: ("W", "kW", "MW"),
    ValueKind.ENERGY: ("Wh", "kWh", "MWh"),
    ValueKind.PERCENT: ("%",),
    ValueKind.DISTANCE: ("km", "m", "mi"),
    ValueKind.TEMPERATURE: ("°C", "°F", "K"),
}
#: Domains whose value of a kind is an attribute (no unit to match).
UNIT_FREE: dict[ValueKind, tuple[str, ...]] = {
    ValueKind.TEMPERATURE: ("climate", "water_heater", "weather"),
}


def binding_specs(capability: Capability) -> dict[str, ParamSpec]:
    """The keys a binding of *capability* accepts, as ``ParamSpec``s."""
    kind = CAPABILITY_KINDS[capability]
    if kind is ValueKind.BOOL:
        return {
            "entity": ParamSpec(str, required=True, entity=True, domains=BOOL_DOMAINS,
                                label="Entity", description="Entity whose state says on / off"),
            "on": ParamSpec(list, advanced=True, items=ParamSpec(str), label="Counts as on",
                            description="States that count as on (default: on, true, home, "
                                        "connected, plugged, …)"),
            "attribute": ParamSpec(str, advanced=True, label="Attribute", attribute_of="entity",
                                   description="Read this attribute instead of the state"),
        }
    if capability is Capability.PHASE_POWER_W:
        return {
            "entities": ParamSpec(list, required=True, label="Power per phase",
                                  items=ParamSpec(str, entity=True, domains=NUMERIC_DOMAINS,
                                                  units=UNITS[ValueKind.POWER]),
                                  description="One power sensor per phase (L1, L2, L3)"),
            "scale": _scale(),
        }
    fit = {"units": UNITS.get(kind, ()), "unit_free_domains": UNIT_FREE.get(kind, ())}
    specs: dict[str, ParamSpec] = {
        "entity": ParamSpec(str, required=True, entity=True, domains=NUMERIC_DOMAINS,
                            label="Entity", description=_KIND_HINT[kind], **fit),
    }
    if kind in (ValueKind.POWER, ValueKind.ENERGY):
        specs["entity"] = ParamSpec(str, required=True, entity=True, domains=NUMERIC_DOMAINS,
                                    label="Entity", description=_KIND_HINT[kind],
                                    when={"import_entity": None}, **fit)
        what = "power" if kind is ValueKind.POWER else "energy"
        specs["import_entity"] = ParamSpec(
            str, advanced=True, entity=True, domains=NUMERIC_DOMAINS, when={"entity": None},
            label=f"Import {what} entity",
            description="Separate import and export sensors instead of one entity", **fit)
        specs["export_entity"] = ParamSpec(
            str, entity=True, domains=NUMERIC_DOMAINS, required=True,
            when={"import_entity": "*"}, label=f"Export {what} entity",
            description=f"Export {what} sensor", **fit)
    if kind is ValueKind.POWER:
        specs["invert_sign"] = ParamSpec(bool, default=False, label="Invert sign",
                                         description="The sensor counts the other direction "
                                                     "as positive")
    specs["attribute"] = ParamSpec(str, advanced=True, label="Attribute", attribute_of="entity",
                                   description="Read this attribute of the entity instead of "
                                               "its state")
    specs["scale"] = _scale()
    return specs


_KIND_HINT = {
    ValueKind.POWER: "Power sensor (W or kW)",
    ValueKind.ENERGY: "Energy counter (Wh or kWh)",
    ValueKind.PERCENT: "Sensor in %",
    ValueKind.DISTANCE: "Distance sensor (km, m or mi)",
    ValueKind.TEMPERATURE: "Temperature sensor, or a climate entity with attribute "
                           "current_temperature",
}


def _scale() -> ParamSpec:
    return ParamSpec(float, advanced=True, default=1.0, label="Scale",
                     description="Multiply the value, e.g. 0.1 for a sensor in tenths")


def bindings_for(capabilities: set[Capability] | frozenset[Capability]
                 ) -> dict[Capability, dict[str, ParamSpec]]:
    """``register_device(bindings=…)`` for *capabilities*."""
    return {cap: binding_specs(cap) for cap in capabilities}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_binding(where: str, capability: Capability, raw: Any) -> ReadBinding:
    """Build a ``ReadBinding`` from a config mapping (raises ``DeviceConfigError``)."""
    if not isinstance(raw, Mapping):
        raise DeviceConfigError([f"{where}: must be a mapping like {{entity: sensor.…}}"])
    kind = CAPABILITY_KINDS[capability]
    specs = binding_specs(capability)
    unknown = set(raw) - set(specs)
    if unknown:
        raise DeviceConfigError([
            f"{where}: unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(specs)})"
        ])
    entity = raw.get("entity") or None
    import_entity = raw.get("import_entity") or None
    export_entity = raw.get("export_entity") or None
    entities = tuple(raw.get("entities") or ())
    if capability is Capability.PHASE_POWER_W:
        if not entities:
            raise DeviceConfigError([f"{where}: needs 'entities: [<L1>, <L2>, <L3>]'"])
    elif import_entity or export_entity:
        if entity:
            raise DeviceConfigError([
                f"{where}: set either 'entity' or 'import_entity' + 'export_entity', not both"])
        if not (import_entity and export_entity):
            raise DeviceConfigError([(f"{where}: 'import_entity' needs 'export_entity' (and "
                                      "the other way round)")])
    elif not entity:
        raise DeviceConfigError([f"{where}: needs 'entity: <entity id>'"])
    scale = raw.get("scale")
    on = raw.get("on")
    return ReadBinding(
        where=where,
        kind=kind,
        entity=str(entity) if entity else None,
        attribute=str(raw["attribute"]) if raw.get("attribute") else None,
        import_entity=str(import_entity) if import_entity else None,
        export_entity=str(export_entity) if export_entity else None,
        entities=tuple(str(e) for e in entities),
        invert_sign=bool(raw.get("invert_sign", False)),
        scale=float(scale) if scale is not None else 1.0,
        on=frozenset(str(v).strip().lower() for v in on) if on else DEFAULT_ON,
    )


def parse_bindings(
    device_id: str, type_name: str, cfg: Mapping[str, Any],
    capabilities: set[Capability] | frozenset[Capability],
) -> dict[Capability, ReadBinding]:
    """Read bindings of the device's bound *capabilities* (others are ignored)."""
    caps = cfg.get("capabilities") or {}
    out: dict[Capability, ReadBinding] = {}
    errors: list[str] = []
    for cap in capabilities:
        raw = caps.get(cap.value)
        if not raw:
            continue
        try:
            out[cap] = parse_binding(
                f"device {device_id!r} ({type_name}): capabilities.{cap.value}", cap, raw)
        except DeviceConfigError as exc:
            errors.extend(exc.errors)
    if errors:
        raise DeviceConfigError(errors)
    return out

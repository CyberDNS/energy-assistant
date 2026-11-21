"""Unified device model: categories, capabilities, params and connections.

Every entry of the ``devices:`` config section is parsed into a
``DeviceSpec``: *what* the device is (``category``), *what it can do*
(``capabilities``, each bound to a data source), its model parameters
(``params``) and how it is linked to other devices (connections).  The plugin
``type:`` says *how* to talk to the device; all remaining keys are the
plugin's own settings (host, profile, …).

Config shape::

    devices:
      - id: main_grid_meter
        category: grid_meter
        type: tasmota                       # plugin
        host: 192.168.1.20                  # plugin settings
        profile: iskra_mt175
        tariff: grid
        params: { max_import_kw: 24 }

      - id: pv_production
        category: pv
        type: generic_homeassistant
        capabilities:
          power_w: { entity: sensor.power_production_pv, invert_sign: true }
        metered_by: main_grid_meter

Each category has a schema (``CATEGORY_SCHEMAS``) with the capabilities it
requires or may bind, the params it accepts and the connections it may have.
``parse_devices`` checks a config against them and raises
``DeviceConfigError`` listing every problem; the plugin-specific checks
(known ``type``, category allowed for that plugin, capabilities provided by
the plugin) are in ``loader/device_loader.validate_plugins``.  The schemas
are also meant to drive a configuration UI later.

Connections
-----------
``metered_by``     the meter this device sits behind.  Builds the metering
                   tree (``core/topology.build_topology``) whose root is the
                   ``grid_meter``.
``controlled_by``  the switch (or charger) that executes this device's plan.
``included_in``    a consumer whose meter already contains this consumer's
                   load (HA Energy dashboard nesting, residual learning).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeviceConfigError(ValueError):
    """The ``devices:`` section is invalid.  ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__(
            "Invalid devices configuration:\n" + "\n".join(f"  - {e}" for e in self.errors)
        )


class DeviceCategory(str, Enum):
    """What a device *is* in the energy system."""

    GRID_METER = "grid_meter"
    METER = "meter"
    PV = "pv"
    BATTERY = "battery"
    VEHICLE = "vehicle"
    CHARGER = "charger"
    THERMAL_LOAD = "thermal_load"
    SWITCH = "switch"
    CONSUMER = "consumer"
    SENSOR = "sensor"

    @property
    def is_meter(self) -> bool:
        """True for the grid meter and sub-meters."""
        return self in (DeviceCategory.GRID_METER, DeviceCategory.METER)


class Capability(str, Enum):
    """A typed thing a device can measure or execute."""

    POWER_W = "power_w"
    ENERGY_IN_KWH = "energy_in_kwh"
    ENERGY_OUT_KWH = "energy_out_kwh"
    PHASE_POWER_W = "phase_power_w"
    SOC_PCT = "soc_pct"
    RANGE_KM = "range_km"
    PLUGGED = "plugged"
    LOCATION_HOME = "location_home"
    TEMPERATURE_C = "temperature_c"
    SWITCH = "switch"
    POWER_SETPOINT = "power_setpoint"
    PHASES = "phases"


class ConnectionType(str, Enum):
    """A typed link from one device to another."""

    METERED_BY = "metered_by"
    CONTROLLED_BY = "controlled_by"
    INCLUDED_IN = "included_in"


# ---------------------------------------------------------------------------
# Category schemas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One model parameter of a category."""

    kind: type
    """``float``, ``int``, ``bool``, ``str``, ``list`` or ``dict``."""
    required: bool = False
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    exclusive_minimum: bool = False
    maximum: float | None = None
    description: str = ""
    validator: Callable[[Any], str | None] | None = None
    """Extra check of the value's content; returns an error text or None."""

    def check(self, value: Any) -> str | None:
        """Return an error text for *value*, or None when it is valid."""
        if self.kind is float:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif self.kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, self.kind)
        if not ok:
            return f"must be a {self.kind.__name__}, got {value!r}"
        if self.choices and value not in self.choices:
            return f"must be one of {', '.join(self.choices)}, got {value!r}"
        too_small = self.minimum is not None and isinstance(value, (int, float)) and (
            value < self.minimum or (self.exclusive_minimum and value == self.minimum)
        )
        if too_small:
            op = ">" if self.exclusive_minimum else ">="
            return f"must be {op} {self.minimum:g}, got {value!r}"
        if self.maximum is not None and isinstance(value, (int, float)) and value > self.maximum:
            return f"must be <= {self.maximum:g}, got {value!r}"
        if self.validator is not None:
            return self.validator(value)
        return None


@dataclass(frozen=True)
class CategorySchema:
    """Rules for one device category."""

    category: DeviceCategory
    description: str
    required_capabilities: frozenset[Capability] = frozenset()
    optional_capabilities: frozenset[Capability] = frozenset()
    params: Mapping[str, ParamSpec] = field(default_factory=dict)
    connections: Mapping[ConnectionType, frozenset[DeviceCategory]] = field(default_factory=dict)
    """Allowed connections and the categories each may point to."""

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.required_capabilities | self.optional_capabilities


def _caps(*names: Capability) -> frozenset[Capability]:
    return frozenset(names)


_C = Capability
_K = DeviceCategory
_METERS = frozenset({_K.GRID_METER, _K.METER})
_METERED = {ConnectionType.METERED_BY: _METERS}

_BATTERY_PARAMS = {
    "capacity_kwh": ParamSpec(float, required=True, minimum=0, exclusive_minimum=True,
                              description="Usable capacity (kWh)"),
    "max_charge_kw": ParamSpec(float, required=True, minimum=0, description="Max charge power (kW)"),
    "max_discharge_kw": ParamSpec(float, required=True, minimum=0,
                                  description="Max discharge power (kW)"),
    "min_soc_pct": ParamSpec(float, minimum=0, maximum=100, description="Lowest planned SoC (%)"),
    "max_soc_pct": ParamSpec(float, minimum=0, maximum=100, description="Highest planned SoC (%)"),
    "charge_efficiency": ParamSpec(float, minimum=0, exclusive_minimum=True, maximum=1),
    "discharge_efficiency": ParamSpec(float, minimum=0, exclusive_minimum=True, maximum=1),
    "efficiency": ParamSpec(str, choices=("configured", "learned"),
                            description="Plan with the configured or the learned round trip"),
    "efficiency_learning": ParamSpec(dict, description="history_days, min_cycles, refit_interval_h"),
    "purchase_price_eur": ParamSpec(float, minimum=0, description="Hardware price, for wear cost"),
    "cycle_life": ParamSpec(int, minimum=1, description="Rated full cycles, for wear cost"),
    "no_grid_charge": ParamSpec(bool, description="Charge from PV surplus only"),
    "standby_w": ParamSpec(float, minimum=0, description="Power treated as standby/noise (W)"),
}

def _check_charge_curve(value: Any) -> str | None:
    for point in value:
        if not isinstance(point, Mapping) or set(point) != {"soc_pct", "efficiency"}:
            return "must be a list of {soc_pct: …, efficiency: …}"
        soc, eff = point["soc_pct"], point["efficiency"]
        if not isinstance(soc, (int, float)) or not 0 < soc <= 100:
            return f"soc_pct must be in (0, 100], got {soc!r}"
        if not isinstance(eff, (int, float)) or not 0 < eff <= 1:
            return f"efficiency must be in (0, 1], got {eff!r}"
    socs = [p["soc_pct"] for p in value]
    if socs != sorted(socs):
        return "points must be sorted by soc_pct"
    return None


def _check_charger_ids(value: Any) -> str | None:
    for key, vid in value.items():
        if not isinstance(key, str) or isinstance(vid, bool) or not isinstance(vid, (int, str)):
            return "must map a charger plugin type to that system's vehicle id, e.g. {openwb_mqtt: 1}"
    return None


def _check_str_list(value: Any) -> str | None:
    if not all(isinstance(v, str) and v for v in value):
        return "must be a list of ids"
    return None


#: ``guest_mode`` values of a charger: what it does with a car that is not a
#: configured vehicle.
GUEST_MODES = ("pv_only", "instant", "off")

_VEHICLE_PARAMS = {
    "capacity_kwh": ParamSpec(float, required=True, minimum=0, exclusive_minimum=True,
                              description="Usable battery capacity (kWh)"),
    "charge_limit_soc_pct": ParamSpec(float, minimum=0, maximum=100,
                                      description="Ceiling the optimizer charges to"),
    "max_charge_kw": ParamSpec(float, minimum=0, exclusive_minimum=True,
                               description="On-board charger limit (kW); default: the charger's"),
    "charge_curve": ParamSpec(list, validator=_check_charge_curve,
                              description="[{soc_pct, efficiency}] charging efficiency per SoC"),
    "timezone": ParamSpec(str, description="Time zone of the weekly plan (default Europe/Berlin)"),
    "charger_ids": ParamSpec(dict, validator=_check_charger_ids,
                             description="This vehicle's id in a charger system, e.g. {openwb_mqtt: 1}"),
    "push_soc_to_charger": ParamSpec(bool, description="Publish the (estimated) SoC to the charger"),
    "refresh_after_min": ParamSpec(float, minimum=1,
                                   description="Press the refresh button when the car's SoC is "
                                               "older than this while charging"),
    "previous_ids": ParamSpec(list, validator=_check_str_list,
                              description="Former asset ids whose stored plans are migrated"),
}

_CHARGER_PARAMS = {
    "min_power_kw": ParamSpec(float, minimum=0, description="Lowest charging power (kW)"),
    "max_power_kw": ParamSpec(float, minimum=0, exclusive_minimum=True,
                              description="Highest charging power (kW)"),
    "default_vehicle": ParamSpec(str, description="Vehicle assigned on plug-in"),
    "guest_mode": ParamSpec(str, choices=GUEST_MODES,
                            description="Behaviour for an unknown car (default pv_only)"),
}

CATEGORY_SCHEMAS: dict[DeviceCategory, CategorySchema] = {
    s.category: s
    for s in (
        CategorySchema(
            _K.GRID_METER,
            "Meter at the grid connection point; root of the metering tree.",
            required_capabilities=_caps(_C.POWER_W),
            optional_capabilities=_caps(_C.ENERGY_IN_KWH, _C.ENERGY_OUT_KWH, _C.PHASE_POWER_W),
            params={
                "max_import_kw": ParamSpec(float, minimum=0, exclusive_minimum=True,
                                           description="Grid connection limit (kW)"),
                "curtail_order": ParamSpec(list,
                                           description="Device ids curtailed first → last"),
                "limit_hysteresis_kw": ParamSpec(float, minimum=0,
                                                 description="Release curtailment this far below"),
            },
        ),
        CategorySchema(
            _K.METER,
            "Sub-meter; defines a metering (tariff) zone.",
            required_capabilities=_caps(_C.POWER_W),
            optional_capabilities=_caps(_C.ENERGY_IN_KWH, _C.ENERGY_OUT_KWH, _C.PHASE_POWER_W),
            connections=_METERED,
        ),
        CategorySchema(
            _K.PV,
            "PV generation (producer).",
            required_capabilities=_caps(_C.POWER_W),
            optional_capabilities=_caps(_C.ENERGY_OUT_KWH),
            connections=_METERED,
        ),
        CategorySchema(
            _K.BATTERY,
            "Stationary battery (storage).",
            required_capabilities=_caps(_C.POWER_W, _C.SOC_PCT),
            optional_capabilities=_caps(_C.POWER_SETPOINT),
            params=_BATTERY_PARAMS,
            connections=_METERED,
        ),
        CategorySchema(
            _K.VEHICLE,
            "Electric vehicle; charged through a charger session.",
            required_capabilities=_caps(_C.SOC_PCT),
            optional_capabilities=_caps(_C.RANGE_KM, _C.PLUGGED, _C.LOCATION_HOME),
            params=_VEHICLE_PARAMS,
        ),
        CategorySchema(
            _K.CHARGER,
            "EV charger; executes a vehicle's charging plan.",
            required_capabilities=_caps(_C.POWER_W, _C.PLUGGED),
            optional_capabilities=_caps(_C.POWER_SETPOINT, _C.PHASES),
            params=_CHARGER_PARAMS,
            connections=_METERED,
        ),
        CategorySchema(
            _K.THERMAL_LOAD,
            "Load kept between two thresholds (aquarium cooler, fridge, heater).",
            required_capabilities=_caps(_C.TEMPERATURE_C),
            # switch: bound on the device itself until thermal loads are
            # controlled_by a switch device of their own.
            optional_capabilities=_caps(_C.SWITCH, _C.POWER_W),
            params={
                "rated_power_w": ParamSpec(float, required=True, minimum=0, exclusive_minimum=True,
                                           description="Power drawn while running (W)"),
            },
            connections={**_METERED, ConnectionType.CONTROLLED_BY: frozenset({_K.SWITCH})},
        ),
        CategorySchema(
            _K.SWITCH,
            "On/off switch (plug, relay); executes another device's plan.",
            required_capabilities=_caps(_C.SWITCH),
            optional_capabilities=_caps(_C.POWER_W, _C.ENERGY_IN_KWH),
            params={"rated_power_w": ParamSpec(float, minimum=0, exclusive_minimum=True)},
            connections=_METERED,
        ),
        CategorySchema(
            _K.CONSUMER,
            "Inflexible load (measured or virtual), with a forecast.",
            optional_capabilities=_caps(_C.POWER_W, _C.ENERGY_IN_KWH, _C.PHASE_POWER_W),
            connections={
                **_METERED,
                ConnectionType.CONTROLLED_BY: frozenset({_K.SWITCH}),
                ConnectionType.INCLUDED_IN: frozenset({_K.CONSUMER}),
            },
        ),
        CategorySchema(
            _K.SENSOR,
            "Input only (temperature, …).",
            optional_capabilities=_caps(_C.TEMPERATURE_C),
        ),
    )
}


# ---------------------------------------------------------------------------
# DeviceSpec
# ---------------------------------------------------------------------------

#: Keys of a device entry that belong to the model; all others are plugin settings.
MODEL_KEYS = frozenset({
    "id", "label", "category", "type", "tariff", "forecast", "learning",
    "capabilities", "params", "ha_discovery", "energy_dashboard",
    *(c.value for c in ConnectionType),
})


@dataclass
class DeviceSpec:
    """One configured device."""

    id: str
    category: DeviceCategory
    type: str
    """Plugin type name: how to talk to the device."""
    label: str | None = None
    tariff: str | None = None
    capabilities: dict[Capability, dict[str, Any]] = field(default_factory=dict)
    """Explicit capability bindings (``{entity: …}``, ``{field: …}``)."""
    params: dict[str, Any] = field(default_factory=dict)
    """Category model parameters (validated against the category schema)."""
    settings: dict[str, Any] = field(default_factory=dict)
    """Plugin settings (host, profile, …), validated by the plugin."""
    metered_by: str | None = None
    controlled_by: str | None = None
    included_in: str | None = None
    forecast: dict[str, Any] | None = None
    learning: bool | dict[str, Any] = True
    """Default learning for consumers (see ``loader/forecast_config``)."""
    ha_discovery: bool = True
    energy_dashboard: str | None = None
    """Statistic id this device was imported from (HA Energy dashboard)."""

    @property
    def display_name(self) -> str:
        return self.label or self.id.replace("_", " ").title()

    def binding(self, capability: Capability) -> dict[str, Any] | None:
        return self.capabilities.get(capability)

    def bound_entity(self, capability: Capability) -> str | None:
        """The HA entity bound to *capability*, if any."""
        b = self.capabilities.get(capability) or {}
        entity = b.get("entity")
        return entity if isinstance(entity, str) and entity else None

    def plugin_config(self) -> dict[str, Any]:
        """The flat dict handed to the plugin factory: settings, params,
        ``category``, ``tariff`` and ``capabilities`` (keyed by name)."""
        cfg: dict[str, Any] = {**self.settings, **self.params}
        cfg["type"] = self.type
        cfg["category"] = self.category.value
        if self.tariff is not None:
            cfg["tariff"] = self.tariff
        cfg["capabilities"] = {c.value: dict(b) for c, b in self.capabilities.items()}
        return cfg

    def to_dict(self) -> dict[str, Any]:
        """Config form of this device (as written in config.yaml)."""
        out: dict[str, Any] = {"id": self.id, "category": self.category.value, "type": self.type}
        if self.label:
            out["label"] = self.label
        if self.tariff:
            out["tariff"] = self.tariff
        out.update(self.settings)
        if self.capabilities:
            out["capabilities"] = {c.value: dict(b) for c, b in self.capabilities.items()}
        if self.params:
            out["params"] = dict(self.params)
        for conn in ConnectionType:
            if (target := getattr(self, conn.value)) is not None:
                out[conn.value] = target
        if self.forecast is not None:
            out["forecast"] = self.forecast
        if self.learning is not True:
            out["learning"] = self.learning
        if not self.ha_discovery:
            out["ha_discovery"] = False
        if self.energy_dashboard:
            out["energy_dashboard"] = self.energy_dashboard
        return out


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def parse_device(device_id: str, raw: Mapping[str, Any], errors: list[str]) -> DeviceSpec | None:
    """Parse one device entry; append problems to *errors*."""
    where = f"device {device_id!r}"
    start = len(errors)

    raw_cat = raw.get("category")
    if raw_cat is None:
        if "role" in raw:
            errors.append(f"{where}: 'role:' was replaced by 'category:' "
                          f"(one of {', '.join(c.value for c in DeviceCategory)})")
        else:
            errors.append(f"{where}: 'category:' is required "
                          f"(one of {', '.join(c.value for c in DeviceCategory)})")
        return None
    try:
        category = DeviceCategory(raw_cat)
    except ValueError:
        errors.append(f"{where}: unknown category {raw_cat!r} "
                      f"(one of {', '.join(c.value for c in DeviceCategory)})")
        return None
    schema = CATEGORY_SCHEMAS[category]

    type_name = raw.get("type")
    if not isinstance(type_name, str) or not type_name:
        errors.append(f"{where}: 'type:' (the plugin) is required")

    # Capabilities
    capabilities: dict[Capability, dict[str, Any]] = {}
    raw_caps = raw.get("capabilities") or {}
    if not isinstance(raw_caps, Mapping):
        errors.append(f"{where}: 'capabilities:' must be a mapping of capability → binding")
        raw_caps = {}
    for name, binding in raw_caps.items():
        try:
            cap = Capability(name)
        except ValueError:
            errors.append(f"{where}: unknown capability {name!r} "
                          f"(one of {', '.join(c.value for c in Capability)})")
            continue
        if cap not in schema.capabilities:
            errors.append(f"{where}: capability {cap.value!r} is not allowed for category "
                          f"{category.value!r} (allowed: {_names(schema.capabilities)})")
            continue
        if not isinstance(binding, Mapping) or not binding:
            errors.append(f"{where}: capability {cap.value!r} needs a binding such as "
                          "{entity: sensor.…}")
            continue
        capabilities[cap] = dict(binding)

    # Params
    params: dict[str, Any] = {}
    raw_params = raw.get("params") or {}
    if not isinstance(raw_params, Mapping):
        errors.append(f"{where}: 'params:' must be a mapping")
        raw_params = {}
    for name, value in raw_params.items():
        spec = schema.params.get(name)
        if spec is None:
            allowed = ", ".join(schema.params) or "none"
            errors.append(f"{where}: unknown param {name!r} for category {category.value!r} "
                          f"(allowed: {allowed})")
            continue
        if (problem := spec.check(value)) is not None:
            errors.append(f"{where}: params.{name} {problem}")
            continue
        params[name] = float(value) if spec.kind is float else value
    for name, spec in schema.params.items():
        if spec.required and name not in raw_params:
            errors.append(f"{where}: params.{name} is required for category {category.value!r}"
                          + (f" ({spec.description})" if spec.description else ""))

    # Connections
    connections: dict[ConnectionType, str] = {}
    for conn in ConnectionType:
        target = raw.get(conn.value)
        if target is None:
            continue
        if conn not in schema.connections:
            errors.append(f"{where}: category {category.value!r} cannot have '{conn.value}'")
            continue
        if not isinstance(target, str) or not target:
            errors.append(f"{where}: '{conn.value}' must be a device id")
            continue
        connections[conn] = target

    settings = {k: v for k, v in raw.items() if k not in MODEL_KEYS}
    if "role" in settings:
        errors.append(f"{where}: 'role:' was replaced by 'category:'")
    if "topology" in settings:
        errors.append(f"{where}: unexpected key 'topology'")
    for name in settings:
        if name in schema.params:
            errors.append(f"{where}: {name!r} is a model parameter — move it under 'params:'")

    forecast = raw.get("forecast")
    if forecast is not None and not isinstance(forecast, Mapping):
        errors.append(f"{where}: 'forecast:' must be a mapping")
    learning = raw.get("learning", True)
    if not isinstance(learning, (bool, Mapping)):
        errors.append(f"{where}: 'learning:' must be true, false or a mapping")
    tariff = raw.get("tariff")
    if tariff is not None and not isinstance(tariff, str):
        errors.append(f"{where}: 'tariff:' must be a tariff id")

    if len(errors) > start:
        return None
    label = raw.get("label")
    return DeviceSpec(
        id=device_id,
        category=category,
        type=str(type_name),
        label=str(label) if label else None,
        tariff=tariff,
        capabilities=capabilities,
        params=params,
        settings=settings,
        metered_by=connections.get(ConnectionType.METERED_BY),
        controlled_by=connections.get(ConnectionType.CONTROLLED_BY),
        included_in=connections.get(ConnectionType.INCLUDED_IN),
        forecast=dict(forecast) if forecast is not None else None,
        learning=dict(learning) if isinstance(learning, Mapping) else learning,
        ha_discovery=raw.get("ha_discovery", True) is not False,
        energy_dashboard=raw.get("energy_dashboard"),
    )


def parse_devices(raw: Mapping[str, Mapping[str, Any]]) -> dict[str, DeviceSpec]:
    """Parse and validate the normalised ``devices:`` section.

    Raises ``DeviceConfigError`` with every problem found.
    """
    errors: list[str] = []
    specs: dict[str, DeviceSpec] = {}
    for device_id, entry in raw.items():
        if not isinstance(entry, Mapping):
            errors.append(f"device {device_id!r}: must be a mapping")
            continue
        spec = parse_device(device_id, entry, errors)
        if spec is not None:
            specs[device_id] = spec
    if not errors:
        errors.extend(validate_connections(specs))
    if errors:
        raise DeviceConfigError(errors)
    return specs


def validate_connections(specs: Mapping[str, DeviceSpec]) -> list[str]:
    """Check connection targets, the metering tree and the grid meter params."""
    errors: list[str] = []
    for spec in specs.values():
        schema = CATEGORY_SCHEMAS[spec.category]
        for conn in ConnectionType:
            target_id = getattr(spec, conn.value)
            if target_id is None:
                continue
            target = specs.get(target_id)
            where = f"device {spec.id!r}: {conn.value} {target_id!r}"
            if target_id == spec.id:
                errors.append(f"{where} points to itself")
            elif target is None:
                errors.append(f"{where} is not a configured device")
            elif target.category not in schema.connections[conn]:
                errors.append(f"{where} is a {target.category.value!r}, expected "
                              f"{_names(schema.connections[conn])}")

        if spec.category is DeviceCategory.THERMAL_LOAD:
            has_switch = Capability.SWITCH in spec.capabilities
            if has_switch and spec.controlled_by:
                errors.append(f"device {spec.id!r}: bind the 'switch' capability or set "
                              "'controlled_by', not both")
            elif not has_switch and not spec.controlled_by:
                errors.append(f"device {spec.id!r}: a thermal_load needs 'controlled_by: "
                              "<switch device>' (or a 'switch' capability binding)")

        if spec.category is DeviceCategory.CHARGER:
            default = spec.params.get("default_vehicle")
            target = specs.get(default) if default else None
            if default and (target is None or target.category is not DeviceCategory.VEHICLE):
                errors.append(f"device {spec.id!r}: params.default_vehicle {default!r} is not a "
                              "configured vehicle")

    grid_meters = [s for s in specs.values() if s.category is DeviceCategory.GRID_METER]
    if len(grid_meters) > 1:
        errors.append("only one grid_meter is allowed, found: "
                      + ", ".join(s.id for s in grid_meters))
    if errors:
        return errors

    # metered_by must form a tree under the grid meter
    for spec in specs.values():
        seen = [spec.id]
        node = spec
        while node.metered_by is not None:
            if node.metered_by in seen:
                errors.append("metered_by forms a loop: " + " → ".join([*seen, node.metered_by]))
                break
            seen.append(node.metered_by)
            node = specs[node.metered_by]
        else:
            if len(seen) > 1 and node.category is not DeviceCategory.GRID_METER:
                errors.append(f"device {spec.id!r}: metered_by chain ends at {node.id!r}, "
                              "which is not the grid_meter (give it 'metered_by' too)")

    for gm in grid_meters:
        for dev_id in gm.params.get("curtail_order", []):
            if dev_id not in specs:
                errors.append(f"device {gm.id!r}: params.curtail_order names unknown "
                              f"device {dev_id!r}")
    return errors


def _names(items: frozenset[Any]) -> str:
    return ", ".join(sorted(i.value for i in items)) or "none"

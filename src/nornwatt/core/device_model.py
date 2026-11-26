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
        category: meter                     # no metered_by: the grid connection
        type: tasmota                       # plugin
        host: 192.168.1.20                  # plugin settings
        profile: iskra_mt175
        tariff: grid
        params: { max_import_kw: 24 }

      - id: pv_production
        category: pv
        type: homeassistant_sensor
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
                   meter without ``metered_by`` — the grid connection.
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
            "Invalid configuration:\n" + "\n".join(f"  - {e}" for e in self.errors)
        )


class DeviceCategory(str, Enum):
    """What a device *is* in the energy system."""

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
        """True for meters (the grid connection meter and sub-meters)."""
        return self is DeviceCategory.METER


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


_NOT_NAMED = object()


@dataclass(frozen=True)
class ParamSpec:
    """One model parameter of a category."""

    kind: type
    """``float``, ``int``, ``bool``, ``str``, ``list``, ``dict`` or ``object``
    (any scalar or list)."""
    required: bool = False
    choices: tuple[str, ...] = ()
    choice_labels: Mapping[str, str] | None = None
    """Readable name per choice for the config UI (default: the value)."""
    minimum: float | None = None
    exclusive_minimum: bool = False
    maximum: float | None = None
    description: str = ""
    validator: Callable[[Any], str | None] | None = None
    """Extra check of the value's content; returns an error text or None."""
    default: Any = None
    """Value used when the key is left out (shown as a hint in the config UI)."""
    secret: bool = False
    """A password or token: belongs in secrets.yaml (``!secret name``) and is
    never sent to the config UI."""
    ref: tuple[str, ...] = ()
    """The value is a device id; the categories it may name (``"*"`` = any).
    The config UI offers those devices in a dropdown."""
    entity: bool = False
    """The value is a Home Assistant entity id."""
    options_from: tuple[str, str] | None = None
    """``(entity key, attribute)``: the config UI offers the values listed in
    that attribute (``options``, ``hvac_modes``, …) of the entity chosen in
    the nearest field named *entity key* (this form or an enclosing one)."""
    attribute_of: str | None = None
    """The value is an attribute name of the entity chosen in the field
    *attribute_of*: the config UI offers that entity's attributes."""
    units: tuple[str, ...] = ()
    """``entity``: the units of measurement a fitting entity has (``W``,
    ``kW``, …).  Only narrows what the config UI suggests; not checked."""
    unit_free_domains: tuple[str, ...] = ()
    """``entity`` with ``units``: domains suggested whatever their unit (e.g.
    a climate entity whose temperature is an attribute)."""
    domains: tuple[str, ...] = ()
    """``entity``: the entity domains it may be (``sensor``, ``switch``, …;
    empty = any, and the value is not checked).  Set: the value must be an
    entity id of one of them, and the config UI only suggests these."""
    tariff: bool = False
    """The value is a tariff id."""
    items: ParamSpec | None = None
    """``list``: the spec of each item."""
    fields: Mapping[str, ParamSpec] | None = None
    """``dict`` with fixed keys: the spec of each key (other keys are errors)."""
    values: ParamSpec | None = None
    """``dict`` with free keys: the spec of each value."""
    keys: ParamSpec | None = None
    """``dict`` with free keys: the spec of each key (e.g. its choices)."""
    plugin: str = ""
    """``dict`` that configures a plugin, selected by its ``type`` key:
    ``"forecast"``.  Checked against that plugin's settings."""
    restart: bool = False
    """A change only takes effect after a restart (not by a config reload)."""
    label: str = ""
    """Short title for the config UI (default: the key)."""
    widget: str = ""
    """Name of a dedicated editor in the config UI: ``load_profile``,
    ``weekdays``, ``time`` / ``time_end`` (quarter hours; ``time_end`` also
    offers 24:00), ``month_day`` (``MM-DD``), ``checkbox`` (a yes/no that is
    off when unset), ``exclusive`` (a yes/no that only one item of the list
    may have: picked in one dropdown for the whole list) …"""
    item_label: str = ""
    """``list`` / ``dict`` with free keys: what one item is called in the
    config UI ("period" → "+ Add period", "Remove period")."""
    summary: str = ""
    """``dict`` item of a list: the title of its card in the config UI, with
    ``{field}`` placeholders (e.g. ``"{name} · {price_eur_per_kwh} €/kWh"``)."""
    choices_from: tuple[str, str] | None = None
    """``(list key, field)``: the config UI offers the *field* values of the
    items of the nearest list named *list key* (e.g. the names of the
    periods).  Only a suggestion; not checked here."""
    initial: Any = None
    """Value a new entry starts with in the config UI (an example to adapt,
    unlike ``default``, which applies when the key is left out)."""
    when: Mapping[str, Any] | None = None
    """The setting only applies while sibling settings have these values:
    ``{key: value}`` or ``{key: (value, …)}``; ``"*"`` = any value, ``None`` =
    not set.  An unset sibling counts with its default.  ``required`` only
    holds while the condition does, and the config UI hides the field
    otherwise."""
    advanced: bool = False
    """Rarely needed: the config UI keeps it under "More settings"."""
    legacy: bool = False
    """Still accepted, but only offered by the config UI where it is in use."""
    selector: bool = False
    """A choice between alternative sets of keys that only the config UI
    shows (e.g. one signed entity or separate import and export entities):
    never written to the file.  The other keys depend on it through ``when``;
    its value is derived from which of them are set (``selected``)."""

    def selected(self, key: str, values: Mapping[str, Any],
                 specs: Mapping[str, ParamSpec]) -> Any:
        """A selector's value for *values*: the first choice whose fields (the
        specs whose ``when`` names *key* with that choice) hold a value, else
        the default.  Mirrored in the config UI (``SchemaForm.selected``)."""
        for choice in self.choices:
            for name, spec in specs.items():
                wanted = (spec.when or {}).get(key, _NOT_NAMED)
                named = wanted == choice or (isinstance(wanted, tuple) and choice in wanted)
                if named and values.get(name) not in (None, "", [], {}):
                    return choice
        return self.default

    def _check_nested(self, value: Any) -> str | None:
        """Check list items and dict keys / values against their specs."""
        if self.items is not None and isinstance(value, list):
            for i, item in enumerate(value):
                if (problem := self.items.check(item)) is not None:
                    return f"[{i + 1}] {problem}"
        if (self.fields is not None and isinstance(value, Mapping)
                and (problems := check_fields(value, self.fields))):
            return "; ".join(problems)
        if self.keys is not None and isinstance(value, Mapping):
            for key in value:
                if (problem := self.keys.check(key)) is not None:
                    return f"key {key!r} {problem}"
        if self.values is not None and isinstance(value, Mapping):
            for key, item in value.items():
                if (problem := self.values.check(item)) is not None:
                    return f"{key}: {problem}"
        return None

    def _check_entity(self, value: str) -> str | None:
        domain, dot, object_id = value.partition(".")
        if not dot or not domain or not object_id:
            return f"must be an entity id like sensor.name, got {value!r}"
        if domain not in self.domains:
            return f"must be a {' / '.join(self.domains)} entity, got {value!r}"
        return None

    def applies(self, values: Mapping[str, Any], specs: Mapping[str, ParamSpec]) -> bool:
        """Whether the ``when`` condition holds for the sibling *values*."""
        for key, wanted in (self.when or {}).items():
            value = values.get(key)
            if key in specs and specs[key].selector:
                value = specs[key].selected(key, values, specs)
            elif value in (None, "") and key in specs:
                value = specs[key].default
            if wanted is None:
                ok = value in (None, "", [], {})
            elif wanted == "*":
                ok = value not in (None, "", [], {})
            else:
                ok = value in wanted if isinstance(wanted, tuple) else value == wanted
            if not ok:
                return False
        return True

    def to_dict(self, key: str = "") -> dict[str, Any]:
        """JSON form for the config UI (``GET /api/config/schema``).  *key* is
        the setting's name; it gives the label when none is set."""
        out: dict[str, Any] = {"kind": self.kind.__name__, "required": self.required}
        if key:
            out["label"] = self.label or humanize(key)
        if self.choices:
            out["choices"] = list(self.choices)
        if self.choice_labels:
            out["choice_labels"] = dict(self.choice_labels)
        if self.minimum is not None:
            out["minimum"] = self.minimum
            out["exclusive_minimum"] = self.exclusive_minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.description:
            out["description"] = self.description
        if self.default is not None:
            out["default"] = self.default
        if self.secret:
            out["secret"] = True
        if self.ref:
            out["ref"] = list(self.ref)
        if self.entity:
            out["entity"] = True
        if self.domains:
            out["domains"] = list(self.domains)
        if self.units:
            out["units"] = list(self.units)
        if self.unit_free_domains:
            out["unit_free_domains"] = list(self.unit_free_domains)
        if self.options_from:
            out["options_from"] = {"entity": self.options_from[0], "attribute": self.options_from[1]}
        if self.attribute_of:
            out["attribute_of"] = self.attribute_of
        if self.tariff:
            out["tariff"] = True
        if self.items is not None:
            out["items"] = self.items.to_dict()
        if self.fields is not None:
            out["fields"] = specs_to_dict(self.fields)
        if self.values is not None:
            out["values"] = self.values.to_dict()
        if self.keys is not None:
            out["keys"] = self.keys.to_dict()
        if self.plugin:
            out["plugin"] = self.plugin
        if self.restart:
            out["restart"] = True
        if self.label:
            out["label"] = self.label
        if self.widget:
            out["widget"] = self.widget
        if self.item_label:
            out["item_label"] = self.item_label
        if self.summary:
            out["summary"] = self.summary
        if self.choices_from:
            out["choices_from"] = {"list": self.choices_from[0], "field": self.choices_from[1]}
        if self.initial is not None:
            out["initial"] = self.initial
        if self.when:
            out["when"] = {k: list(v) if isinstance(v, tuple) else v for k, v in self.when.items()}
        if self.advanced:
            out["advanced"] = True
        if self.legacy:
            out["legacy"] = True
        if self.selector:
            out["selector"] = True
        if self.kind is object:
            out["kind"] = "any"
        return out

    def check(self, value: Any) -> str | None:
        """Return an error text for *value*, or None when it is valid."""
        if self.selector:
            return "is only a switch in the config UI: leave it out"
        if self.kind is object:
            ok = isinstance(value, (str, int, float, bool, list, Mapping))
        elif self.kind is float:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif self.kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, self.kind)
        if not ok:
            return f"must be a {self.kind.__name__}, got {value!r}"
        if (problem := self._check_nested(value)) is not None:
            return problem
        if self.choices and value not in self.choices:
            return f"must be one of {', '.join(self.choices)}, got {value!r}"
        if (self.entity and self.domains and isinstance(value, str)
                and (problem := self._check_entity(value))):
            return problem
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


_UNITS = (
    ("_eur_per_kwh", "€/kWh"), ("_per_h", "per h"), ("_kwh", "kWh"), ("_kwp", "kWp"),
    ("_kw", "kW"), ("_pct", "%"), ("_eur", "€"), ("_min", "min"), ("_minutes", "min"),
    ("_hours", "h"), ("_days", "days"), ("_w", "W"), ("_v", "V"), ("_a", "A"), ("_s", "s"),
    ("_h", "h"), ("_c", "°C"), ("_km", "km"),
)
_WORDS = {"soc": "SoC", "ha": "Home Assistant", "id": "ID", "ids": "IDs", "url": "URL",
          "api": "API", "pv": "PV", "ev": "EV", "mqtt": "MQTT", "kwp": "kWp"}


def humanize(key: str) -> str:
    """A readable label for a config key: ``max_charge_kw`` → "Max charge (kW)"."""
    unit = ""
    for suffix, text in _UNITS:
        if key.endswith(suffix) and len(key) > len(suffix):
            key, unit = key[: -len(suffix)], f" ({text})"
            break
    words = [_WORDS.get(w, w) for w in key.split("_") if w]
    if words and words[0].islower():
        words[0] = words[0].capitalize()
    return " ".join(words) + unit


def specs_to_dict(specs: Mapping[str, ParamSpec]) -> dict[str, Any]:
    """``{key: spec}`` in JSON form, each with its label."""
    return {name: spec.to_dict(name) for name, spec in specs.items()}


#: Display names of the capabilities.
CAPABILITY_TITLES = {
    "power_w": "Power", "energy_in_kwh": "Energy consumed", "energy_out_kwh": "Energy fed in",
    "phase_power_w": "Power per phase", "soc_pct": "State of charge", "range_km": "Range",
    "plugged": "Plugged in", "location_home": "At home", "temperature_c": "Temperature",
    "switch": "On/off switch", "power_setpoint": "Power control", "phases": "Phase switching",
}


def file_keys(specs: Mapping[str, ParamSpec]) -> list[str]:
    """The keys of *specs* that can be in the file (not the UI-only selectors)."""
    return [name for name, spec in specs.items() if not spec.selector]


def check_fields(
    values: Mapping[str, Any], specs: Mapping[str, ParamSpec], *, what: str = "key"
) -> list[str]:
    """Problems of the mapping *values* against *specs*: unknown keys, wrong
    values and missing required keys.  An empty value counts as left out."""
    problems: list[str] = []
    for name, value in values.items():
        spec = specs.get(name)
        if spec is None:
            problems.append(f"unknown {what} {name!r} (allowed: {', '.join(file_keys(specs)) or 'none'})")
        elif value is None:
            continue
        elif (problem := spec.check(value)) is not None:
            problems.append(f"{name} {problem}")
    for name, spec in specs.items():
        if spec.required and values.get(name) in (None, "") and spec.applies(values, specs):
            problems.append(f"{what} {name!r} is required"
                            + (f" ({spec.description})" if spec.description else ""))
    return problems


@dataclass(frozen=True)
class CategorySchema:
    """Rules for one device category."""

    category: DeviceCategory
    description: str
    title: str = ""
    """Display name (default: the category name, humanized)."""
    required_capabilities: frozenset[Capability] = frozenset()
    optional_capabilities: frozenset[Capability] = frozenset()
    params: Mapping[str, ParamSpec] = field(default_factory=dict)
    connections: Mapping[ConnectionType, frozenset[DeviceCategory]] = field(default_factory=dict)
    """Allowed connections and the categories each may point to."""

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.required_capabilities | self.optional_capabilities

    def to_dict(self) -> dict[str, Any]:
        """JSON form for the config UI (``GET /api/config/schema``)."""
        return {
            "category": self.category.value,
            "title": self.title or humanize(self.category.value),
            "description": self.description,
            "required_capabilities": sorted(c.value for c in self.required_capabilities),
            "optional_capabilities": sorted(c.value for c in self.optional_capabilities),
            "params": specs_to_dict(self.params),
            "connections": {
                conn.value: sorted(c.value for c in targets)
                for conn, targets in self.connections.items()
            },
        }


def _caps(*names: Capability) -> frozenset[Capability]:
    return frozenset(names)


_C = Capability
_K = DeviceCategory
_METERS = frozenset({_K.METER})
_METERED = {ConnectionType.METERED_BY: _METERS}

_BATTERY_PARAMS = {
    "capacity_kwh": ParamSpec(float, label="Usable capacity (kWh)", required=True, minimum=0, exclusive_minimum=True,
                              description="Usable capacity (kWh)"),
    "max_charge_kw": ParamSpec(float, label="Max charge power (kW)", required=True, minimum=0, description="Max charge power (kW)"),
    "max_discharge_kw": ParamSpec(float, label="Max discharge power (kW)", required=True, minimum=0,
                                  description="Max discharge power (kW)"),
    "min_soc_pct": ParamSpec(float, default=0, label="Lowest SoC (%)", minimum=0, maximum=100, description="Lowest planned SoC (%)"),
    "max_soc_pct": ParamSpec(float, default=100, label="Highest SoC (%)", minimum=0, maximum=100, description="Highest planned SoC (%)"),
    "charge_efficiency": ParamSpec(float, default=0.95, description="Share of the charged energy that ends up stored", minimum=0, exclusive_minimum=True, maximum=1),
    "discharge_efficiency": ParamSpec(float, default=0.95, description="Share of the stored energy that comes out", minimum=0, exclusive_minimum=True, maximum=1),
    "efficiency": ParamSpec(str, default="configured", label="Efficiency used for planning", choices=("configured", "learned"),
                            description="Plan with the configured or the learned round trip"),
    "efficiency_learning": ParamSpec(dict, when={"efficiency": "learned"}, advanced=True, label="Efficiency learning", description="How the round trip is learned", fields={
        "history_days": ParamSpec(float, minimum=1, default=14, description="Days of history used"),
        "min_cycles": ParamSpec(float, minimum=0, default=3,
                                description="Full cycles needed before an estimate counts"),
        "refit_interval_h": ParamSpec(float, minimum=0, exclusive_minimum=True, default=6,
                                      description="Hours between refits"),
    }),
    "purchase_price_eur": ParamSpec(float, label="Purchase price (€)", minimum=0, description="Hardware price, for wear cost"),
    "cycle_life": ParamSpec(int, label="Cycle life (full cycles)", minimum=1, description="Rated full cycles, for wear cost"),
    "no_grid_charge": ParamSpec(bool, default=False, label="Charge from PV only", description="Charge from PV surplus only"),
    "own_meter": ParamSpec(str, default="zone", advanced=True, label="Follows on its own",
                           choices=("zone", "grid_connection"),
                           description="Meter the battery regulates on without Nornwatt: its zone's, or the grid connection point's (then PV a separately billed load uses is not available to it)"),
    "standby_w": ParamSpec(float, default=50, advanced=True, label="Standby threshold (W)", minimum=0, description="Power treated as standby/noise (W)"),
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
    "charge_limit_soc_pct": ParamSpec(float, label="Charge limit (%)", minimum=0, maximum=100,
                                      description="Ceiling the optimizer charges to"),
    "max_charge_kw": ParamSpec(float, minimum=0, exclusive_minimum=True,
                               description="On-board charger limit (kW); default: the charger's"),
    "charge_curve": ParamSpec(
        list, label="Charge curve", validator=_check_charge_curve, item_label="point",
        description="Charging efficiency up to each SoC, ascending (the taper near full)",
        items=ParamSpec(dict, fields={
            "soc_pct": ParamSpec(float, required=True, minimum=0, exclusive_minimum=True,
                                 maximum=100, label="up to SoC (%)"),
            "efficiency": ParamSpec(float, required=True, minimum=0, exclusive_minimum=True,
                                    maximum=1, label="efficiency (0–1)"),
        }),
    ),
    "timezone": ParamSpec(str, advanced=True, default="Europe/Berlin", description="Time zone of the weekly plan (default Europe/Berlin)"),
    "charger_ids": ParamSpec(dict, label="ID in the charger system", item_label="charger system", validator=_check_charger_ids, values=ParamSpec(object),
                             description="This vehicle's id in a charger system, e.g. openwb_mqtt → 1"),
    "push_soc_to_charger": ParamSpec(bool, default=False, label="Send SoC to the charger", description="Publish the (estimated) SoC to the charger"),
    "refresh_after_min": ParamSpec(float, label="Refresh when SoC is older than (min)", minimum=1,
                                   description="Press the refresh button when the car's SoC is "
                                               "older than this while charging"),
}

_CHARGER_PARAMS = {
    "min_power_kw": ParamSpec(float, label="Lowest charging power (kW)", minimum=0, description="Lowest charging power (kW)"),
    "max_power_kw": ParamSpec(float, label="Highest charging power (kW)", minimum=0, exclusive_minimum=True,
                              description="Highest charging power (kW)"),
    "default_vehicle": ParamSpec(str, label="Default vehicle", ref=("vehicle",), description="Vehicle assigned on plug-in"),
    "guest_mode": ParamSpec(str, default="pv_only", label="Guest car charging", choices=GUEST_MODES,
                            description="Behaviour for an unknown car (default pv_only)"),
}

_THERMAL_PARAMS = {
    "bottom_threshold": ParamSpec(float, label="Lower threshold", required=True,
                                  description="Lower bound of the allowed range (e.g. °C)"),
    "top_threshold": ParamSpec(float, label="Upper threshold", required=True,
                               description="Upper bound of the allowed range"),
    "direction": ParamSpec(str, default="reduces", label="Running the device", choices=("reduces", "increases"),
                           description="reduces (cooler, default) or increases (heater) the value"),
    "unit": ParamSpec(str, default="°C", advanced=True, label="Display unit", description="Display unit (default °C)"),
    "active_rate_per_h": ParamSpec(float, label="Change per hour while running", required=True, minimum=0, exclusive_minimum=True,
                                   description="Change per hour while running (fallback model)"),
    "drift_rate_per_h": ParamSpec(float, label="Drift per hour while off", required=True, minimum=0,
                                  description="Drift per hour while off (fallback model)"),
    "min_runtime_h": ParamSpec(float, default=0, label="Minimum run time (h)", minimum=0, description="Minimum on-time (compressor)"),
    "min_offtime_h": ParamSpec(float, default=0, label="Minimum off time (h)", minimum=0, description="Minimum off-time (compressor)"),
    "rated_power_w": ParamSpec(float, label="Power while running (W)", minimum=0, exclusive_minimum=True,
                               description="For a device that switches itself (a thermostat); "
                                           "with controlled_by the switch's rated power is used"),
}

#: Params of the grid connection meter (the meter without ``metered_by``).
_GRID_ONLY = {"metered_by": None}
_GRID_PARAMS = {
    "max_import_kw": ParamSpec(float, minimum=0, exclusive_minimum=True, when=_GRID_ONLY,
                               label="Grid connection limit (kW)",
                               description="The plan and the control loop keep the import below it"),
    "curtail_order": ParamSpec(list, items=ParamSpec(str, ref=("*",)), when=_GRID_ONLY, item_label="device to curtail",
                               label="Curtail order",
                               description="Devices reduced first → last when the limit is "
                                           "reached (default: batteries, then chargers)"),
    "limit_hysteresis_kw": ParamSpec(float, minimum=0, when=_GRID_ONLY, advanced=True,
                                     label="Limit hysteresis (kW)",
                                     description="Release the curtailment this far below the "
                                                 "limit (default: 5 % of it)"),
}

CATEGORY_SCHEMAS: dict[DeviceCategory, CategorySchema] = {
    s.category: s
    for s in (
        CategorySchema(
            _K.METER,
            "Electricity meter. The one meter that is not behind another is the grid "
            "connection; the others are sub-meters and define a tariff zone.",
            title="Meter",
            required_capabilities=_caps(_C.POWER_W),
            optional_capabilities=_caps(_C.ENERGY_IN_KWH, _C.ENERGY_OUT_KWH, _C.PHASE_POWER_W),
            params=_GRID_PARAMS,
            connections=_METERED,
        ),
        CategorySchema(
            _K.PV,
            "PV generation (producer).",
            title="PV system",
            required_capabilities=_caps(_C.POWER_W),
            optional_capabilities=_caps(_C.ENERGY_OUT_KWH),
            connections=_METERED,
        ),
        CategorySchema(
            _K.BATTERY,
            "Stationary battery (storage).",
            title="Battery",
            required_capabilities=_caps(_C.POWER_W, _C.SOC_PCT),
            optional_capabilities=_caps(_C.POWER_SETPOINT),
            params=_BATTERY_PARAMS,
            connections=_METERED,
        ),
        CategorySchema(
            _K.VEHICLE,
            "Electric vehicle; charged through a charger session.",
            title="Vehicle",
            required_capabilities=_caps(_C.SOC_PCT),
            optional_capabilities=_caps(_C.RANGE_KM, _C.PLUGGED, _C.LOCATION_HOME),
            params=_VEHICLE_PARAMS,
        ),
        CategorySchema(
            _K.CHARGER,
            "EV charger; executes a vehicle's charging plan.",
            title="Charger",
            required_capabilities=_caps(_C.POWER_W, _C.PLUGGED),
            optional_capabilities=_caps(_C.POWER_SETPOINT, _C.PHASES),
            params=_CHARGER_PARAMS,
            connections=_METERED,
        ),
        CategorySchema(
            _K.THERMAL_LOAD,
            "Load kept between two thresholds (aquarium cooler, fridge, heater); "
            "switched by the switch it is controlled_by, or by itself (a thermostat).",
            title="Thermal load",
            required_capabilities=_caps(_C.TEMPERATURE_C),
            optional_capabilities=_caps(_C.POWER_W, _C.POWER_SETPOINT),
            params=_THERMAL_PARAMS,
            connections={**_METERED, ConnectionType.CONTROLLED_BY: frozenset({_K.SWITCH})},
        ),
        CategorySchema(
            _K.SWITCH,
            "On/off switch (plug, relay); executes another device's plan.",
            title="Switch",
            required_capabilities=_caps(_C.SWITCH),
            optional_capabilities=_caps(_C.POWER_W, _C.ENERGY_IN_KWH),
            params={"rated_power_w": ParamSpec(float, label="Power while on (W)", minimum=0, exclusive_minimum=True,
                                               description="Power drawn by the load while on (W)")},
            connections=_METERED,
        ),
        CategorySchema(
            _K.CONSUMER,
            "Inflexible load (measured or virtual), with a forecast.",
            title="Consumer",
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
            title="Sensor",
            optional_capabilities=_caps(_C.TEMPERATURE_C),
        ),
    )
}


# ---------------------------------------------------------------------------
# Control mode (observe / active)
# ---------------------------------------------------------------------------

#: ``control:`` of a device: Nornwatt plans it but only *observes*
#: (logs what it would send, sends nothing), or controls it (*active*).
OBSERVE = "observe"
ACTIVE = "active"
CONTROL_MODES = (OBSERVE, ACTIVE)

#: Categories the control loop commands — the only ones ``control:`` is for.
CONTROLLED_CATEGORIES = frozenset({
    DeviceCategory.BATTERY, DeviceCategory.CHARGER, DeviceCategory.THERMAL_LOAD,
})


# ---------------------------------------------------------------------------
# DeviceSpec
# ---------------------------------------------------------------------------

#: Keys of a device entry that belong to the model; all others are plugin settings.
MODEL_KEYS = frozenset({
    "id", "label", "category", "type", "tariff", "forecast", "learning",
    "capabilities", "params", "ha_discovery", "energy_dashboard", "control",
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
    control: str | None = None
    """``observe`` / ``active`` (``CONTROL_MODES``) for a device the control
    loop commands.  Not set = ``active``: configurations from before observe
    mode keep controlling their devices.  Devices added from the UI, presets
    or the onboarding are written with ``control: observe``."""

    @property
    def controlled(self) -> bool:
        """Whether the control loop commands this device."""
        return self.category in CONTROLLED_CATEGORIES

    @property
    def observed(self) -> bool:
        """A controlled device in observe mode: planned, never commanded."""
        return self.controlled and self.control == OBSERVE

    @property
    def display_name(self) -> str:
        return self.label or self.id.replace("_", " ").title()

    def binding(self, capability: Capability) -> dict[str, Any] | None:
        return self.capabilities.get(capability)

    def bound_entity(self, capability: Capability) -> str | None:
        """The HA entity bound to *capability*, if its state is the value as is.

        ``None`` when nothing is bound, or the binding reads an attribute,
        scales the value or combines entities: the entity's history (learned
        forecasts and models) would then not be the capability's value.
        """
        b = self.capabilities.get(capability) or {}
        if any(b.get(key) not in (None, "", 1, 1.0) for key in ("attribute", "scale")):
            return None
        entity = b.get("entity")
        return entity if isinstance(entity, str) and entity else None

    def plugin_config(self) -> dict[str, Any]:
        """The flat dict handed to the plugin factory: settings, params,
        ``category``, ``tariff``, connections and ``capabilities`` (keyed by name)."""
        cfg: dict[str, Any] = {**self.settings, **self.params}
        cfg["type"] = self.type
        cfg["category"] = self.category.value
        if self.tariff is not None:
            cfg["tariff"] = self.tariff
        for conn in ConnectionType:
            if (target := getattr(self, conn.value)) is not None:
                cfg[conn.value] = target
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
        if self.control:
            out["control"] = self.control
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
    if raw_cat == "grid_meter":
        errors.append(f"{where}: category 'grid_meter' was merged into 'meter' — write "
                      "'category: meter' (the meter without 'metered_by' is the grid connection)")
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
        if spec is None and name == "previous_ids":
            errors.append(f"{where}: params.previous_ids is no longer used — remove it (stored "
                          "plans were moved to the vehicle long ago)")
            continue
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
    control = raw.get("control")
    if control is not None:
        if control not in CONTROL_MODES:
            errors.append(f"{where}: 'control:' must be one of {', '.join(CONTROL_MODES)}")
        elif category not in CONTROLLED_CATEGORIES:
            errors.append(f"{where}: 'control:' is only for devices Nornwatt commands "
                          f"({_names(CONTROLLED_CATEGORIES)})")

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
        control=control,
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


def _thermal_load_errors(spec: DeviceSpec, specs: Mapping[str, DeviceSpec]) -> list[str]:
    """A thermal load needs a switch with a rated power (or its own rated
    power when it switches itself), and a valid range."""
    errors: list[str] = []
    where = f"device {spec.id!r}"
    bottom, top = spec.params.get("bottom_threshold"), spec.params.get("top_threshold")
    if isinstance(bottom, (int, float)) and isinstance(top, (int, float)) and bottom >= top:
        errors.append(f"{where}: params.bottom_threshold ({bottom:g}) must be below "
                      f"top_threshold ({top:g})")
    if not spec.controlled_by:
        if spec.params.get("rated_power_w") is None:
            errors.append(f"{where}: a thermal_load needs 'controlled_by: <switch device>', "
                          "or 'params: {rated_power_w: …}' when it switches itself "
                          "(a thermostat)")
        return errors
    switch = specs.get(spec.controlled_by)
    if (switch is not None and switch.category is DeviceCategory.SWITCH
            and switch.params.get("rated_power_w") is None):
        errors.append(f"{where}: its switch {switch.id!r} needs 'params: {{rated_power_w: …}}' "
                      "(the power the load draws while on)")
    return errors


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
            errors.extend(_thermal_load_errors(spec, specs))

        if spec.category is DeviceCategory.CHARGER:
            default = spec.params.get("default_vehicle")
            target = specs.get(default) if default else None
            if default and (target is None or target.category is not DeviceCategory.VEHICLE):
                errors.append(f"device {spec.id!r}: params.default_vehicle {default!r} is not a "
                              "configured vehicle")

    controllers: dict[str, list[str]] = {}
    for spec in specs.values():
        if spec.controlled_by:
            controllers.setdefault(spec.controlled_by, []).append(spec.id)
    for switch_id, users in controllers.items():
        if len(users) > 1:
            errors.append(f"device {switch_id!r} controls more than one device: "
                          + ", ".join(users))

    grid_meters = [s for s in specs.values() if is_grid_meter(s)]
    if len(grid_meters) > 1:
        errors.append("only one meter can be the grid connection (the meter without "
                      "'metered_by'), found: " + ", ".join(s.id for s in grid_meters)
                      + " — give the sub-meters 'metered_by: <meter>'")
    for spec in specs.values():
        if spec.category is DeviceCategory.METER and spec.metered_by is not None:
            for name in _GRID_PARAMS:
                if name in spec.params:
                    errors.append(f"device {spec.id!r}: params.{name} belongs to the grid "
                                  "connection meter (the meter without 'metered_by')")
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
            if len(seen) > 1 and not is_grid_meter(node):
                errors.append(f"device {spec.id!r}: metered_by chain ends at {node.id!r}, "
                              "which is not a meter")

    for gm in grid_meters:
        for dev_id in gm.params.get("curtail_order", []):
            if dev_id not in specs:
                errors.append(f"device {gm.id!r}: params.curtail_order names unknown "
                              f"device {dev_id!r}")
    return errors


def is_grid_meter(spec: DeviceSpec) -> bool:
    """True for the grid connection meter: the meter that is behind no other."""
    return spec.category is DeviceCategory.METER and spec.metered_by is None


def _names(items: frozenset[Any]) -> str:
    return ", ".join(sorted(i.value for i in items)) or "none"

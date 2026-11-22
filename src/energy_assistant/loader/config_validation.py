"""Checks of the config sections other than ``devices:``, and of the
forecast / learning options inside devices.

``validate_sections(raw)`` returns every problem in the settings sections
(``core/config_schema.SECTION_SCHEMAS``) and in ``tariffs:`` / ``forecasts:``
(against the plugin each entry names).  ``forecast_errors`` /
``learning_errors`` check a device's ``forecast:`` and ``learning:``.

The configuration UI builds its forms from the same specs
(``GET /api/config/schema``), so what the UI offers is what the loader accepts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.config_schema import (
    COLLECTIONS,
    LEARNING_KINDS,
    SECTION_SCHEMAS,
    THERMAL_LEARNING,
)
from ..core.device_model import DeviceSpec, ParamSpec, check_fields
from ..plugins import registry as plugin_registry
from ..plugins.learned import LEARNING_OPTIONS

#: Top-level sections the loader knows.
KNOWN_SECTIONS = ("devices", *SECTION_SCHEMAS, *COLLECTIONS)


def learning_options(kind: str) -> Mapping[str, ParamSpec]:
    """The options of ``learning:`` for a consumer or a thermal load."""
    return THERMAL_LEARNING if kind == "thermal_load" else LEARNING_OPTIONS


def _plugin_info(kind: str, type_name: Any) -> Any:
    if not isinstance(type_name, str):
        return None
    return (plugin_registry.tariff_info(type_name) if kind == "tariff"
            else plugin_registry.forecast_info(type_name))


def _plugin_types(kind: str, scope: str = "") -> list[str]:
    infos = plugin_registry.tariff_infos() if kind == "tariff" else plugin_registry.forecast_infos()
    return [i.type_name for i in infos if not scope or scope in i.scopes]


def plugin_entry_errors(
    where: str,
    cfg: Any,
    kind: str,
    *,
    scope: str = "",
    common: Mapping[str, ParamSpec] | None = None,
) -> list[str]:
    """Problems of one tariff or forecast configuration."""
    if not isinstance(cfg, Mapping):
        return [f"{where}: must be a mapping with a 'type'"]
    type_name = cfg.get("type")
    info = _plugin_info(kind, type_name)
    if info is None or (scope and scope not in info.scopes):
        known = ", ".join(_plugin_types(kind, scope))
        return [f"{where}: unknown {kind} type {type_name!r} (one of {known})"]
    if info.settings is None:
        return []
    specs = {**info.settings, **(common or {})}
    values = {k: v for k, v in cfg.items() if k != "type"}
    errors = [f"{where}: {p}" for p in check_fields(values, specs, what="setting")]
    # Nested plugin configurations (a learned forecast's fallback).
    for name, spec in specs.items():
        if spec.plugin and isinstance(values.get(name), Mapping):
            errors.extend(plugin_entry_errors(f"{where}: {name}", values[name], spec.plugin,
                                              scope="device" if spec.plugin == "forecast" else ""))
    return errors


def forecast_errors(spec: DeviceSpec) -> list[str]:
    """Problems of a device's ``forecast:``."""
    if spec.forecast is None:
        return []
    return plugin_entry_errors(f"device {spec.id!r}: forecast", spec.forecast, "forecast",
                               scope="device")


def learning_errors(spec: DeviceSpec) -> list[str]:
    """Problems of a device's ``learning:`` options."""
    if not isinstance(spec.learning, Mapping):
        return []
    kind = LEARNING_KINDS.get(spec.category)
    if kind is None:
        return [(f"device {spec.id!r}: 'learning:' options are only for consumers and thermal "
                 "loads (use true or false)")]
    return [f"device {spec.id!r}: learning: {p}"
            for p in check_fields(spec.learning, learning_options(kind), what="option")]


def validate_sections(raw: Mapping[str, Any]) -> list[str]:
    """Every problem in the sections other than ``devices:``."""
    errors: list[str] = []
    for name in raw:
        # Removed sections (topology, assets) get their own migration hints.
        if name not in KNOWN_SECTIONS and name not in ("topology", "assets"):
            errors.append(f"unknown section {name!r} (known: {', '.join(KNOWN_SECTIONS)})")

    for name, schema in SECTION_SCHEMAS.items():
        section = raw.get(name)
        if section is None:
            continue
        if not isinstance(section, Mapping):
            errors.append(f"{name}: must be a mapping")
            continue
        # backends.iobroker is ignored with a warning by the loader.
        values = {k: v for k, v in section.items() if not (name == "backends" and k == "iobroker")}
        errors.extend(f"{name}: {p}" for p in check_fields(values, schema.fields, what="setting"))
        learning = values.get("learning") if name == "energy_dashboard" else None
        if isinstance(learning, Mapping):
            errors.extend(f"{name}: learning: {p}" for p in
                          check_fields(learning, LEARNING_OPTIONS, what="option"))

    for name, collection in COLLECTIONS.items():
        entries = _entries(raw.get(name))
        if entries is None:
            errors.append(f"{name}: must be a mapping of id → settings")
            continue
        for entry_id, cfg in entries.items():
            errors.extend(plugin_entry_errors(
                f"{name}: {entry_id}", cfg, collection.plugin_kind,
                scope=collection.scope, common=collection.common,
            ))
    tariffs = _entries(raw.get("tariffs")) or {}
    defaults = [t for t, cfg in tariffs.items() if isinstance(cfg, Mapping) and cfg.get("default")]
    if len(defaults) > 1:
        errors.append(f"tariffs: only one tariff can be the default, found: {', '.join(defaults)}")
    dashboard_tariff = (raw.get("energy_dashboard") or {}).get("tariff") \
        if isinstance(raw.get("energy_dashboard"), Mapping) else None
    if dashboard_tariff and dashboard_tariff not in tariffs:
        errors.append(f"energy_dashboard: tariff {dashboard_tariff!r} is not a configured tariff")
    return errors


def _entries(section: Any) -> dict[str, Any] | None:
    """A collection section as ``{id: cfg}`` (list form with ``id`` keys accepted)."""
    if section is None:
        return {}
    if isinstance(section, Mapping):
        return dict(section)
    if isinstance(section, list):
        return {
            str(e["id"]): {k: v for k, v in e.items() if k != "id"}
            for e in section if isinstance(e, Mapping) and e.get("id")
        }
    return None

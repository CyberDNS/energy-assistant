"""homeassistant_sensor plugin — a meter, PV system, consumer or sensor read from HA.

Device type: ``homeassistant_sensor`` (old name: ``generic_homeassistant``)

Read only.  Every capability is bound to Home Assistant entities the same
way (``plugins/_homeassistant/bindings``)::

    - id: grid
      category: meter
      type: homeassistant_sensor
      capabilities:
        power_w:        { import_entity: sensor.grid_import_w, export_entity: sensor.grid_export_w }
        energy_in_kwh:  { entity: sensor.grid_energy_import }
        energy_out_kwh: { entity: sensor.grid_energy_export }
        phase_power_w:  { entities: [sensor.l1_w, sensor.l2_w, sensor.l3_w] }

    - id: pv_production
      category: pv
      type: homeassistant_sensor
      capabilities:
        power_w: { entity: sensor.power_production_pv, invert_sign: true }

    - id: outdoor
      category: sensor
      type: homeassistant_sensor
      capabilities:
        temperature_c: { entity: weather.home, attribute: temperature }

A consumer without a power sensor is ``type: generic_consumer``.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory, DeviceConfigError
from ...core.device_model import DeviceCategory as C
from ...core.plugin_registry import BuildContext, PluginRegistry
from .._homeassistant.bindings import bindings_for, parse_bindings

_log = logging.getLogger(__name__)

TYPE = "homeassistant_sensor"
LEGACY_TYPE = "generic_homeassistant"

_BINDABLE = frozenset({
    Cap.POWER_W, Cap.ENERGY_IN_KWH, Cap.ENERGY_OUT_KWH, Cap.PHASE_POWER_W, Cap.TEMPERATURE_C,
})

#: Keys of the flat config before capability bindings (#97).
_OLD_KEYS = ("oid_power", "power", "oid_power_import", "power_import",
             "oid_power_export", "power_export", "invert_sign")


def register(registry: PluginRegistry) -> None:
    common: dict[str, Any] = {
        "categories": {C.METER, C.PV, C.CONSUMER, C.SENSOR},
        "bindable": _BINDABLE,
        "bindings": bindings_for(_BINDABLE),
        "settings": {},
    }
    registry.register_device(
        TYPE, _build,
        title="Home Assistant sensors",
        description="Power, energy, per-phase power or temperature from Home Assistant "
                    "entities (read only).",
        **common,
    )
    registry.register_device(
        LEGACY_TYPE, _build,
        title="Home Assistant sensors (old name)",
        legacy=True,
        **common,
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import HASensorDevice

    type_name = str(cfg.get("type") or TYPE)
    old = [k for k in _OLD_KEYS if k in cfg]
    if old:
        raise DeviceConfigError([(
            f"device {device_id!r} ({type_name}): {', '.join(old)} moved to "
            "'capabilities: {power_w: {entity: …, invert_sign: …}}' "
            "(or {import_entity: …, export_entity: …})"
        )])
    bindings = parse_bindings(device_id, type_name, cfg, _BINDABLE)
    category = DeviceCategory(cfg["category"])
    if not bindings:
        hint = (" (a consumer without a power sensor is type: generic_consumer)"
                if category is C.CONSUMER else "")
        raise DeviceConfigError([(
            f"device {device_id!r} ({type_name}) needs at least one capability bound, "
            f"e.g. 'capabilities: {{power_w: {{entity: …}}}}'{hint}"
        )])
    if ctx.ha_client is None:
        _log.warning("Device %r (%s) requires Home Assistant backend — skipping",
                     device_id, type_name)
        return None
    return HASensorDevice(device_id, category, ctx.ha_client, bindings)

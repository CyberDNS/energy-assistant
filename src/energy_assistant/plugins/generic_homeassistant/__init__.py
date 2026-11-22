"""generic_homeassistant plugin — reads a power entity from Home Assistant.

The entity is bound to the ``power_w`` capability::

    - id: pv_production
      category: pv
      type: generic_homeassistant
      capabilities:
        power_w: { entity: sensor.power_production_pv, invert_sign: true }

    - id: grid
      category: meter
      type: generic_homeassistant
      capabilities:
        power_w: { import_entity: sensor.grid_import_w, export_entity: sensor.grid_export_w }
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory, DeviceConfigError, ParamSpec
from ...core.device_model import DeviceCategory as C
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "generic_homeassistant", _build,
        title="Home Assistant power sensor",
        categories={C.METER, C.PV, C.CONSUMER},
        bindable={Cap.POWER_W},
        settings={},
        binding={
            "entity": ParamSpec(str, entity=True, label="Power sensor",
                                description="Sensor in W or kW"),
            "import_entity": ParamSpec(str, advanced=True, entity=True,
                                       description="Import power sensor (with export_entity, "
                                                   "instead of entity)"),
            "export_entity": ParamSpec(str, advanced=True, entity=True, description="Export power sensor"),
            "invert_sign": ParamSpec(bool, default=False, label="Invert sign",
                                     description="The sensor counts production as positive"),
        },
    )


_OLD_KEYS = ("oid_power", "power", "oid_power_import", "power_import",
             "oid_power_export", "power_export", "invert_sign")


def power_binding(cfg: dict[str, Any]) -> dict[str, Any]:
    """The ``capabilities.power_w`` binding of a device config (may be empty)."""
    return dict((cfg.get("capabilities") or {}).get("power_w") or {})


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import GenericHADevice

    old = [k for k in _OLD_KEYS if k in cfg]
    if old:
        raise DeviceConfigError([(
            f"device {device_id!r} (generic_homeassistant): {', '.join(old)} moved to "
            "'capabilities: {power_w: {entity: …, invert_sign: …}}' "
            "(or {import_entity: …, export_entity: …})"
        )])
    binding = power_binding(cfg)
    if not binding:
        raise DeviceConfigError([(
            f"device {device_id!r} (generic_homeassistant) needs "
            "'capabilities: {power_w: {entity: …}}' (a consumer without a power sensor "
            "is type: generic_consumer)"
        )])
    unknown = set(binding) - {"entity", "import_entity", "export_entity", "invert_sign"}
    if unknown:
        raise DeviceConfigError([(
            f"device {device_id!r}: capabilities.power_w: unknown key(s) "
            f"{', '.join(sorted(unknown))} (entity, import_entity, export_entity, invert_sign)"
        )])
    if ctx.ha_client is None:
        _log.warning(
            "Device %r (generic_homeassistant) requires Home Assistant backend — skipping",
            device_id,
        )
        return None

    try:
        return GenericHADevice(
            device_id=device_id,
            category=DeviceCategory(cfg["category"]),
            client=ctx.ha_client,
            entity_power=binding.get("entity"),
            entity_power_import=binding.get("import_entity"),
            entity_power_export=binding.get("export_entity"),
            invert_sign=bool(binding.get("invert_sign", False)),
        )
    except ValueError as exc:
        raise DeviceConfigError([f"device {device_id!r}: {exc}"]) from exc

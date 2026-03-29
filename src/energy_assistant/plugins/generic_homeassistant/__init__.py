"""generic_homeassistant plugin — reads a power entity from Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import parse_device_role
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device("generic_homeassistant", _build)


def _get(cfg: dict[str, Any], *keys: str) -> Any:
    """Return the first non-None value found under any of *keys*."""
    for k in keys:
        if (v := cfg.get(k)) is not None:
            return v
    return None


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import GenericHADevice
    if ctx.ha_client is None:
        _log.warning(
            "Device %r (generic_homeassistant) requires Home Assistant backend — skipping",
            device_id,
        )
        return None
    try:
        return GenericHADevice(
            device_id=device_id,
            role=parse_device_role(cfg.get("role")),
            client=ctx.ha_client,
            entity_power=_get(cfg, "oid_power", "power"),
            entity_power_import=_get(cfg, "oid_power_import", "power_import"),
            entity_power_export=_get(cfg, "oid_power_export", "power_export"),
            invert_sign=bool(cfg.get("invert_sign", False)),
        )
    except ValueError as exc:
        _log.warning("Device %r: %s — skipping", device_id, exc)
        return None
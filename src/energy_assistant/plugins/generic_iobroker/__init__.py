"""generic_iobroker plugin — reads a power OID from ioBroker."""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import parse_device_role
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device("generic_iobroker", _build)


def _get(cfg: dict[str, Any], *keys: str) -> Any:
    """Return the first non-None value found under any of *keys*."""
    for k in keys:
        if (v := cfg.get(k)) is not None:
            return v
    return None


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import GenericIoBrokerDevice
    if ctx.iobroker_pool is None or ctx.backends.iobroker is None:
        _log.warning("Device %r (generic_iobroker) requires ioBroker backend — skipping", device_id)
        return None
    iob = ctx.backends.iobroker
    client = ctx.iobroker_pool.get(host=iob.host, port=iob.port, api_token=iob.api_token)
    try:
        return GenericIoBrokerDevice(
            device_id=device_id,
            role=parse_device_role(cfg.get("role")),
            client=client,
            # Accept both flat (oid_power) and nested-source (power) key names.
            oid_power=_get(cfg, "oid_power", "power"),
            oid_power_import=_get(cfg, "oid_power_import", "power_import"),
            oid_power_export=_get(cfg, "oid_power_export", "power_export"),
        )
    except ValueError as exc:
        _log.warning("Device %r: %s — skipping", device_id, exc)
        return None
"""differential plugin — derives power as minuend − subtrahend."""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import parse_device_role
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    # Deferred: needs other devices to be built first.
    registry.register_device("differential", _build, deferred=True)


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import DifferentialDevice
    if ctx.device_registry is None:
        _log.error("Differential %r: device_registry not in context", device_id)
        return None
    minuend_id = cfg.get("minuend")
    subtrahend_id = cfg.get("subtrahend")
    if not minuend_id or not subtrahend_id:
        _log.warning(
            "Differential %r: 'minuend' and 'subtrahend' required — skipping", device_id
        )
        return None
    minuend = ctx.device_registry.get(minuend_id)
    if minuend is None:
        _log.warning("Differential %r: minuend %r not found — skipping", device_id, minuend_id)
        return None
    subtrahend = ctx.device_registry.get(subtrahend_id)
    if subtrahend is None:
        _log.warning("Differential %r: subtrahend %r not found — skipping", device_id, subtrahend_id)
        return None
    min_w = cfg.get("min_w")
    max_w = cfg.get("max_w")
    return DifferentialDevice(
        device_id=device_id,
        role=parse_device_role(cfg.get("role")),
        minuend=minuend,
        subtrahend=subtrahend,
        minuend_field=cfg.get("minuend_field", "power_w"),
        subtrahend_field=cfg.get("subtrahend_field", "power_w"),
        min_power_w=float(min_w) if min_w is not None else None,
        max_power_w=float(max_w) if max_w is not None else None,
    )
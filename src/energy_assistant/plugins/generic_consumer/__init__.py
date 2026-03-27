"""generic_consumer plugin — virtual consumer device for load profile modelling."""

from __future__ import annotations

from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry


def register(registry: PluginRegistry) -> None:
    registry.register_device("generic_consumer", _build)


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object:
    from .device import GenericConsumerDevice

    return GenericConsumerDevice(
        device_id=device_id,
        tariff_id=cfg.get("tariff"),
    )

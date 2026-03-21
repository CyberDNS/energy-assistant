"""sma_modbus_iobroker plugin — SMA Sunny Boy Storage battery via ioBroker Modbus adapter."""

from __future__ import annotations

import logging
from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device("sma_modbus_iobroker", _build)


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import SmaSunnyBoyStorageDevice

    if ctx.iobroker_pool is None or ctx.backends.iobroker is None:
        _log.warning(
            "Device %r (sma_modbus_iobroker) requires ioBroker backend — skipping", device_id
        )
        return None
    iob = ctx.backends.iobroker
    client = ctx.iobroker_pool.get(host=iob.host, port=iob.port, api_token=iob.api_token)
    return SmaSunnyBoyStorageDevice(
        device_id=device_id,
        client=client,
        modbus_instance=cfg.get("modbus_instance", "modbus.0"),
        capacity_kwh=float(cfg.get("capacity_kwh", 0.0)),
        max_charge_kw=float(cfg.get("max_charge_kw", 0.0)),
        max_discharge_kw=float(cfg.get("max_discharge_kw", 0.0)),
        voltage_max_v=float(cfg.get("voltage_max_v", 253.0)),
        voltage_nominal_v=float(cfg.get("voltage_nominal_v", 230.0)),
    )

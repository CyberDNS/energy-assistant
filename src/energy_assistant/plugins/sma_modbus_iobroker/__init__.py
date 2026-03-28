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
    purchase_price = cfg.get("purchase_price_eur")
    cycle_life = cfg.get("cycle_lifetime") or cfg.get("cycle_life")
    return SmaSunnyBoyStorageDevice(
        device_id=device_id,
        client=client,
        modbus_instance=cfg.get("modbus_instance", "modbus.0"),
        capacity_kwh=float(cfg.get("capacity_kwh", 0.0)),
        min_soc_pct=float(cfg.get("min_soc_pct", 0.0)),
        max_soc_pct=float(cfg.get("max_soc_pct", 100.0)),
        max_charge_kw=float(cfg.get("max_charge_kw", 0.0)),
        max_discharge_kw=float(cfg.get("max_discharge_kw", 0.0)),
        voltage_max_v=float(cfg.get("voltage_max_v", 253.0)),
        voltage_nominal_v=float(cfg.get("voltage_nominal_v", 230.0)),
        purchase_price_eur=float(purchase_price) if purchase_price is not None else None,
        cycle_life=int(cycle_life) if cycle_life is not None else None,
        no_grid_charge=bool(cfg.get("no_grid_charge", False)),
    )

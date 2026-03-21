"""zendure_iobroker plugin — Zendure SolarFlow battery via ioBroker."""

from __future__ import annotations

import logging
from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device("zendure_iobroker", _build)


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import ZendureIoBrokerDevice
    if ctx.iobroker_pool is None or ctx.backends.iobroker is None:
        _log.warning("Device %r (zendure_iobroker) requires ioBroker backend — skipping", device_id)
        return None
    hub_id = cfg.get("hub_id", "")
    device_serial = cfg.get("device_serial", "")
    if not hub_id or not device_serial:
        _log.warning(
            "Device %r (zendure_iobroker): 'hub_id' and 'device_serial' required — skipping",
            device_id,
        )
        return None
    iob = ctx.backends.iobroker
    client = ctx.iobroker_pool.get(host=iob.host, port=iob.port, api_token=iob.api_token)
    return ZendureIoBrokerDevice(
        device_id=device_id,
        client=client,
        hub_id=hub_id,
        device_serial=device_serial,
        capacity_kwh=float(cfg.get("capacity_kwh", 0.0)),
        max_charge_kw=float(cfg.get("max_charge_kw", 0.0)),
        max_discharge_kw=float(cfg.get("max_discharge_kw", 0.0)),
        maintenance_charge_w=float(cfg.get("maintenance_charge_w", 300.0)),
    )
"""tibber_iobroker plugin — Tibber spot-price tariff and live-power device."""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import parse_device_role
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_tariff("tibber_iobroker", _build_tariff)
    registry.register_device("tibber_iobroker", _build_device)


def _build_tariff(tariff_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .tariff import TibberIoBrokerTariff
    if ctx.iobroker_pool is None or ctx.backends.iobroker is None:
        _log.warning("Tariff %r (tibber_iobroker) requires ioBroker backend — skipping", tariff_id)
        return None
    home_id = cfg.get("home_id", "")
    if not home_id:
        _log.warning("Tariff %r (tibber_iobroker): 'home_id' is required — skipping", tariff_id)
        return None
    iob = ctx.backends.iobroker
    client = ctx.iobroker_pool.get(host=iob.host, port=iob.port, api_token=iob.api_token)
    return TibberIoBrokerTariff(tariff_id=tariff_id, client=client, home_id=home_id)


def _build_device(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import TibberIoBrokerDevice
    if ctx.iobroker_pool is None or ctx.backends.iobroker is None:
        _log.warning("Device %r (tibber_iobroker) requires ioBroker backend — skipping", device_id)
        return None
    home_id = cfg.get("home_id", "")
    if not home_id:
        _log.warning("Device %r (tibber_iobroker): 'home_id' is required — skipping", device_id)
        return None
    iob = ctx.backends.iobroker
    client = ctx.iobroker_pool.get(host=iob.host, port=iob.port, api_token=iob.api_token)
    return TibberIoBrokerDevice(
        device_id=device_id,
        role=parse_device_role(cfg.get("role"), default=parse_device_role("meter")),
        client=client,
        home_id=home_id,
    )
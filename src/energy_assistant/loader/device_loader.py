"""DeviceLoader — builds a DeviceRegistry and topology from an AppConfig.

All plugin knowledge lives in ``energy_assistant.plugins``.
This module is intentionally generic: it contains no plugin-specific logic.
Adding a new device or tariff type requires only:

1. Creating the plugin under ``plugins/<name>/``
2. Registering it in ``plugins/__init__.py``

No changes to this file are needed.

Two-pass build
--------------
1. First pass builds all device types that are **not** declared as deferred.
2. Second pass builds deferred device types (e.g. ``differential``) after
   the first pass has fully populated the device registry.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.config import AppConfig
from ..core.plugin_registry import BuildContext
from ..core.registry import DeviceRegistry
from ..core.tariff import TariffModel
from ..core.topology import TopologyNode, build_topology
from ..plugins import registry as plugin_registry
from ..plugins._homeassistant.client import HAClient
from ..plugins._iobroker.pool import IoBrokerConnectionPool

_log = logging.getLogger(__name__)


def build(
    app_config: AppConfig,
) -> tuple[DeviceRegistry, dict[str, TariffModel], TopologyNode | None]:
    """Build runtime objects from *app_config*.

    Returns
    -------
    registry:
        All registered devices.
    tariffs:
        All configured tariff models keyed by name.
    topology:
        Root of the topology tree, or ``None`` when not configured.
    """
    # --- Backend clients ---
    iobroker_pool: IoBrokerConnectionPool | None = None
    ha_client: HAClient | None = None

    if app_config.backends.iobroker:
        iobroker_pool = IoBrokerConnectionPool()

    if app_config.backends.homeassistant:
        ha_cfg = app_config.backends.homeassistant
        ha_client = HAClient(
            url=ha_cfg.url,
            token=ha_cfg.token,
            timeout=ha_cfg.timeout_s,
        )

    ctx = BuildContext(
        backends=app_config.backends,
        iobroker_pool=iobroker_pool,
        ha_client=ha_client,
    )

    # --- Tariffs ---
    tariffs: dict[str, TariffModel] = {}
    for name, cfg in app_config.tariffs.items():
        tariff = plugin_registry.build_tariff(name, cfg, ctx)
        if tariff is not None:
            tariffs[name] = tariff

    # --- Devices (two-pass) ---
    device_registry = DeviceRegistry()
    deferred: list[tuple[str, dict[str, Any]]] = []

    for device_id, cfg in app_config.devices.items():
        type_name = cfg.get("type", "")
        if plugin_registry.is_deferred(type_name):
            deferred.append((device_id, cfg))
            continue
        device = plugin_registry.build_device(device_id, cfg, ctx)
        if device is not None:
            device_registry.register(device)

    # Second pass — deferred devices can read the populated device_registry.
    ctx.device_registry = device_registry
    for device_id, cfg in deferred:
        device = plugin_registry.build_device(device_id, cfg, ctx)
        if device is not None:
            device_registry.register(device)

    # --- Topology ---
    topology = build_topology(app_config.topology)
    return device_registry, tariffs, topology

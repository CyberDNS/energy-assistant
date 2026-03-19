"""DeviceLoader — builds a DeviceRegistry and topology from an AppConfig.

This module is the bridge between the 6-section YAML config and the
runtime ``DeviceRegistry``.  It understands all built-in source and
tariff plugin types and constructs the appropriate objects.

Two-pass device build
---------------------
Devices are built in two passes:

1. **First pass** — all devices whose ``source.type`` is *not*
   ``"differential"`` are built and registered.
2. **Second pass** — differential devices are built by looking up their
   named source devices from the already-populated registry.

This ensures that a ``DifferentialDevice`` can always find its sources
regardless of declaration order in the YAML file.

Supported source types
----------------------
``generic_iobroker``
    Reads power from ioBroker via the simple-api.
    Required: ``power`` OR both ``power_import`` + ``power_export``.

``generic_homeassistant``
    Reads power from a Home Assistant entity.
    Required: ``power`` OR both ``power_import`` + ``power_export``.

``differential``
    Derives power as ``minuend − subtrahend`` from two named devices.
    Required: ``minuend``,  ``subtrahend``.
    Optional: ``minuend_field`` (default ``"power_w"``),
              ``subtrahend_field`` (default ``"power_w"``),
              ``min_w``, ``max_w``.

Supported tariff types
----------------------
``flat_rate``
    Constant price.  Required: ``price_eur_per_kwh``.

``tibber_iobroker``
    Dynamic Tibber spot prices via the ioBroker tibberlink adapter.
    Required: ``home_id``.  Uses the global ``backends.iobroker`` connection.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.config import AppConfig, BackendsConfig
from ..core.models import DeviceRole
from ..core.registry import DeviceRegistry
from ..core.tariff import TariffModel
from ..core.topology import TopologyNode, build_topology
from ..plugins._homeassistant.client import HAClient
from ..plugins._iobroker.pool import IoBrokerConnectionPool
from ..plugins.differential.device import DifferentialDevice
from ..plugins.flat_rate.tariff import FlatRateTariff
from ..plugins.generic_homeassistant.device import GenericHADevice
from ..plugins.generic_iobroker.device import GenericIoBrokerDevice
from ..plugins.tibber_iobroker.device import TibberIoBrokerDevice
from ..plugins.tibber_iobroker.tariff import TibberIoBrokerTariff

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

    # --- Tariffs ---
    tariffs = _build_all_tariffs(
        app_config.tariffs,
        app_config.backends,
        iobroker_pool,
    )

    # --- Devices (two-pass for differential) ---
    registry = _build_all_devices(app_config, iobroker_pool, ha_client)

    # --- Topology ---
    topology = build_topology(app_config.topology)

    return registry, tariffs, topology


# ---------------------------------------------------------------------------
# Tariff builders
# ---------------------------------------------------------------------------


def _build_all_tariffs(
    tariffs_cfg: dict[str, dict[str, Any]],
    backends: BackendsConfig,
    iobroker_pool: IoBrokerConnectionPool | None,
) -> dict[str, TariffModel]:
    result: dict[str, TariffModel] = {}
    for name, cfg in tariffs_cfg.items():
        tariff = _build_tariff(name, cfg, backends, iobroker_pool)
        if tariff is not None:
            result[name] = tariff
    return result


def _build_tariff(
    name: str,
    cfg: dict[str, Any],
    backends: BackendsConfig,
    iobroker_pool: IoBrokerConnectionPool | None,
) -> TariffModel | None:
    tariff_type = cfg.get("type", "")

    if tariff_type == "flat_rate":
        import_price = float(cfg.get("import_price_eur_per_kwh", 0.0))
        export_price = float(cfg.get("export_price_eur_per_kwh", 0.0))
        return FlatRateTariff(
            tariff_id=name,
            import_price_eur_per_kwh=import_price,
            export_price_eur_per_kwh=export_price,
        )

    if tariff_type == "tibber_iobroker":
        if iobroker_pool is None or backends.iobroker is None:
            _log.warning("Tariff %r requires ioBroker backend — skipping", name)
            return None
        iob = backends.iobroker
        client = iobroker_pool.get(
            host=iob.host,
            port=iob.port,
            api_token=iob.api_token,
        )
        home_id = cfg.get("home_id", "")
        if not home_id:
            _log.warning("Tariff %r (tibber_iobroker): 'home_id' is required — skipping", name)
            return None
        return TibberIoBrokerTariff(tariff_id=name, client=client, home_id=home_id)

    _log.warning("Unknown tariff type %r for %r — skipping", tariff_type, name)
    return None


# ---------------------------------------------------------------------------
# Device builders
# ---------------------------------------------------------------------------


def _build_all_devices(
    app_config: AppConfig,
    iobroker_pool: IoBrokerConnectionPool | None,
    ha_client: HAClient | None,
) -> DeviceRegistry:
    registry = DeviceRegistry()
    deferred: list[tuple[str, dict[str, Any]]] = []

    # First pass: everything except differential devices
    for device_id, cfg in app_config.devices.items():
        src_cfg = cfg.get("source", {})
        if src_cfg.get("type") == "differential":
            deferred.append((device_id, cfg))
            continue

        device = _build_device(
            device_id,
            cfg,
            app_config.backends,
            iobroker_pool,
            ha_client,
        )
        if device is not None:
            registry.register(device)

    # Second pass: differential devices (dependencies must already be in registry)
    for device_id, cfg in deferred:
        device = _build_differential_device(device_id, cfg, registry)
        if device is not None:
            registry.register(device)

    return registry


def _parse_role(device_id: str, cfg: dict[str, Any]) -> DeviceRole:
    raw = cfg.get("role", "consumer")
    try:
        return DeviceRole(raw)
    except ValueError:
        _log.warning(
            "Device %r: unknown role %r — defaulting to CONSUMER", device_id, raw
        )
        return DeviceRole.CONSUMER


def _build_device(
    device_id: str,
    cfg: dict[str, Any],
    backends: BackendsConfig,
    iobroker_pool: IoBrokerConnectionPool | None,
    ha_client: HAClient | None,
) -> object | None:
    role = _parse_role(device_id, cfg)
    src_cfg = cfg.get("source", {})
    src_type = src_cfg.get("type", "")

    if src_type == "generic_iobroker":
        if iobroker_pool is None or backends.iobroker is None:
            _log.warning("Device %r requires ioBroker backend — skipping", device_id)
            return None
        iob = backends.iobroker
        client = iobroker_pool.get(
            host=iob.host,
            port=iob.port,
            api_token=iob.api_token,
        )
        try:
            return GenericIoBrokerDevice(
                device_id=device_id,
                role=role,
                client=client,
                oid_power=src_cfg.get("power"),
                oid_power_import=src_cfg.get("power_import"),
                oid_power_export=src_cfg.get("power_export"),
            )
        except ValueError as exc:
            _log.warning("Device %r: %s — skipping", device_id, exc)
            return None

    if src_type == "generic_homeassistant":
        if ha_client is None:
            _log.warning("Device %r requires Home Assistant backend — skipping", device_id)
            return None
        try:
            return GenericHADevice(
                device_id=device_id,
                role=role,
                client=ha_client,
                entity_power=src_cfg.get("power"),
                entity_power_import=src_cfg.get("power_import"),
                entity_power_export=src_cfg.get("power_export"),
            )
        except ValueError as exc:
            _log.warning("Device %r: %s — skipping", device_id, exc)
            return None

    if src_type == "tibber_iobroker":
        if iobroker_pool is None or backends.iobroker is None:
            _log.warning("Device %r requires ioBroker backend — skipping", device_id)
            return None
        home_id = src_cfg.get("home_id", "")
        if not home_id:
            _log.warning("Device %r (tibber_iobroker): 'home_id' is required — skipping", device_id)
            return None
        iob = backends.iobroker
        client = iobroker_pool.get(
            host=iob.host,
            port=iob.port,
            api_token=iob.api_token,
        )
        return TibberIoBrokerDevice(
            device_id=device_id,
            role=role,
            client=client,
            home_id=home_id,
        )

    _log.warning(
        "Device %r: unknown source type %r — skipping", device_id, src_type
    )
    return None


def _build_differential_device(
    device_id: str,
    cfg: dict[str, Any],
    registry: DeviceRegistry,
) -> object | None:
    role = _parse_role(device_id, cfg)
    src_cfg = cfg.get("source", {})

    minuend_id = src_cfg.get("minuend")
    subtrahend_id = src_cfg.get("subtrahend")

    if not minuend_id or not subtrahend_id:
        _log.warning(
            "Differential device %r: 'minuend' and 'subtrahend' are required — skipping",
            device_id,
        )
        return None

    minuend = registry.get(minuend_id)
    if minuend is None:
        _log.warning(
            "Differential device %r: minuend device %r not found — skipping",
            device_id,
            minuend_id,
        )
        return None

    subtrahend = registry.get(subtrahend_id)
    if subtrahend is None:
        _log.warning(
            "Differential device %r: subtrahend device %r not found — skipping",
            device_id,
            subtrahend_id,
        )
        return None

    min_w = src_cfg.get("min_w")
    max_w = src_cfg.get("max_w")

    return DifferentialDevice(
        device_id=device_id,
        role=role,
        minuend=minuend,
        subtrahend=subtrahend,
        minuend_field=src_cfg.get("minuend_field", "power_w"),
        subtrahend_field=src_cfg.get("subtrahend_field", "power_w"),
        min_power_w=float(min_w) if min_w is not None else None,
        max_power_w=float(max_w) if max_w is not None else None,
    )

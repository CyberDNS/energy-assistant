"""DeviceLoader — builds a DeviceRegistry and topology from an AppConfig.

All plugin knowledge lives in ``energy_assistant.plugins``.
This module is intentionally generic: it contains no plugin-specific logic.
Adding a new device or tariff type requires only:

1. Creating the plugin under ``plugins/<name>/``
2. Registering it in ``plugins/__init__.py``

No changes to this file are needed.

Validation
----------
Before anything is built, ``validate_plugins`` checks each device against
its plugin's metadata (``core/plugin_registry``): the ``type`` exists, the
plugin can serve the device's category, every bound capability can be bound
by that plugin, and the category's required capabilities are provided.  All
problems are raised together as one ``DeviceConfigError``.

Two-pass build
--------------
1. First pass builds all device types that are **not** declared as deferred.
2. Second pass builds deferred device types (e.g. ``differential``) after
   the first pass has fully populated the device registry.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from ..core.config import AppConfig
from ..core.device_model import CATEGORY_SCHEMAS, DeviceConfigError, DeviceSpec
from ..core.plugin_registry import BuildContext
from ..core.registry import DeviceRegistry
from ..core.tariff import TariffModel
from ..core.topology import TopologyNode, build_topology
from ..plugins import registry as plugin_registry
from ..plugins._homeassistant.client import HAClient
from ..plugins._iobroker.pool import IoBrokerConnectionPool
from .forecast_config import effective_forecast_configs

_log = logging.getLogger(__name__)


def make_build_context(app_config: AppConfig) -> BuildContext:
    """Create a ``BuildContext`` with live backend connections from *app_config*.

    Call this once and share the resulting context across all build functions
    so that all plugins reuse the same backend client instances.
    """
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

    return BuildContext(
        backends=app_config.backends,
        iobroker_pool=iobroker_pool,
        ha_client=ha_client,
    )


def build_top_level_forecasts(
    app_config: AppConfig,
    ctx: BuildContext | None = None,
) -> list:
    """Build forecast providers declared in the top-level ``forecasts:`` section.

    These are system-wide forecasts (e.g. PV generation) rather than
    per-device consumption profiles.  Both types are returned aggregated by
    ``build_all_forecasts``.

    Parameters
    ----------
    app_config:
        Parsed application configuration.
    ctx:
        Build context with backend clients.  When ``None`` a minimal context
        is created; providers that require live backends (e.g. ``pvforecast_iobroker``)
        will be skipped.

    Returns
    -------
    list:
        Forecast provider instances built from ``app_config.forecasts``.
    """
    if ctx is None:
        ctx = BuildContext(backends=app_config.backends)

    providers = []
    for forecast_id, cfg in app_config.forecasts.items():
        try:
            provider = plugin_registry.build_forecast(forecast_id, cfg, ctx)
            if provider is not None:
                providers.append(provider)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Could not build top-level forecast %r: %s", forecast_id, exc)
    return providers


def build_all_forecasts(
    app_config: AppConfig,
    ctx: BuildContext | None = None,
) -> list:
    """Build all forecast providers — top-level and per-device.

    Combines the results of ``build_top_level_forecasts`` and
    ``build_device_forecasts`` into a single list.  All providers sharing
    the same ``BuildContext`` so they reuse backend connections.
    """
    return build_top_level_forecasts(app_config, ctx) + build_device_forecasts(app_config, ctx)


def build_device_forecast_map(
    app_config: AppConfig,
    ctx: BuildContext | None = None,
) -> dict[str, object]:
    """Like ``build_device_forecasts`` but keyed by device id.

    Lets callers that need one particular device's forecast (e.g. the
    tariff-weighted price curve) reuse the instance that planning already
    uses, instead of building a second one — which matters for providers
    that hold state, such as a fitted ``learned`` model.
    """
    if ctx is None:
        ctx = BuildContext(backends=app_config.backends)

    providers: dict[str, object] = {}
    # Default learning for basic consumers and ``subtract: [auto]`` are
    # applied here (see ``forecast_config``).
    for device_id, forecast_cfg in effective_forecast_configs(app_config).items():
        try:
            provider = plugin_registry.build_forecast(f"{device_id}_forecast", forecast_cfg, ctx)
            if provider is not None:
                providers[device_id] = provider
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Could not build forecast for device %r: %s", device_id, exc
            )
    return providers


def build_device_forecasts(
    app_config: AppConfig,
    ctx: BuildContext | None = None,
) -> list:
    """Build forecast providers declared on individual consumer devices.

    Iterates through all device configs and, for each device that has a
    ``forecast:`` sub-section, builds the corresponding forecast provider
    via the plugin registry.

    This is separate from the top-level ``forecasts:`` section — device-level
    forecasts model the consumption profile of that specific device and are
    aggregated into the ``ForecastQuantity.CONSUMPTION`` forecast at plan time.

    Parameters
    ----------
    app_config:
        Parsed application configuration.
    ctx:
        Build context with backend clients.  When ``None`` a minimal context
        without backend connections is created (sufficient for providers like
        ``static_profile`` that need no live data).

    Returns
    -------
    list:
        Forecast provider instances for each device that declared one.
    """
    return list(build_device_forecast_map(app_config, ctx).values())


def validate_plugins(devices: Mapping[str, DeviceSpec]) -> None:
    """Check *devices* against their plugins' metadata.

    Raises ``DeviceConfigError`` listing every problem.
    """
    errors: list[str] = []
    for spec in devices.values():
        where = f"device {spec.id!r}"
        info = plugin_registry.device_info(spec.type)
        if info is None:
            errors.append(f"{where}: unknown type {spec.type!r} "
                          f"(one of {', '.join(plugin_registry.device_types())})")
            continue
        if info.categories and spec.category not in info.categories:
            allowed = ", ".join(sorted(c.value for c in info.categories))
            errors.append(f"{where}: plugin {spec.type!r} cannot be a "
                          f"{spec.category.value!r} (allowed: {allowed})")
            continue
        for cap in spec.capabilities:
            if cap not in info.bindable:
                bindable = ", ".join(sorted(c.value for c in info.bindable)) or "none"
                errors.append(f"{where}: plugin {spec.type!r} cannot bind capability "
                              f"{cap.value!r} (bindable: {bindable})")
        provided = info.provides | set(spec.capabilities)
        missing = CATEGORY_SCHEMAS[spec.category].required_capabilities - provided
        for cap in sorted(missing, key=lambda c: c.value):
            hint = (f" — bind it under 'capabilities: {{{cap.value}: {{entity: …}}}}'"
                    if cap in info.bindable else "")
            errors.append(f"{where}: category {spec.category.value!r} requires capability "
                          f"{cap.value!r}, which plugin {spec.type!r} does not provide{hint}")
    if errors:
        raise DeviceConfigError(errors)


def build(
    app_config: AppConfig,
    ctx: BuildContext | None = None,
) -> tuple[DeviceRegistry, dict[str, TariffModel], TopologyNode | None]:
    """Build runtime objects from *app_config*.

    Parameters
    ----------
    ctx:
        Optional pre-built ``BuildContext``.  When omitted a new context is
        created internally.  Pass a shared context (from ``make_build_context``)
        to ensure all devices reuse the same backend connection pool.

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
    if ctx is None:
        ctx = make_build_context(app_config)

    # --- Tariffs ---
    tariffs: dict[str, TariffModel] = {}
    for name, cfg in app_config.tariffs.items():
        tariff = plugin_registry.build_tariff(name, cfg, ctx)
        if tariff is not None:
            tariffs[name] = tariff

    # --- Devices (validated, then built in two passes) ---
    validate_plugins(app_config.devices)
    device_registry = DeviceRegistry()
    deferred: list[DeviceSpec] = []

    def _build_one(spec: DeviceSpec) -> None:
        device = plugin_registry.build_device(spec.id, spec.plugin_config(), ctx)
        if device is None:
            return
        if device.category != spec.category:
            raise DeviceConfigError([(
                f"device {spec.id!r}: plugin {spec.type!r} built a "
                f"{device.category.value!r}, but the config says {spec.category.value!r}"
            )])
        device_registry.register(device)

    for spec in app_config.devices.values():
        if plugin_registry.is_deferred(spec.type):
            deferred.append(spec)
            continue
        _build_one(spec)

    # Second pass — deferred devices can read the populated device_registry.
    ctx.device_registry = device_registry
    for spec in deferred:
        _build_one(spec)

    # --- Topology (from the devices' metered_by connections) ---
    topology = build_topology(app_config.devices)
    return device_registry, tariffs, topology

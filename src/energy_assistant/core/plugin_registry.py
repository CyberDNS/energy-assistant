"""PluginRegistry — maps type-name strings to factory callables.

All device and tariff plugin types register themselves here.
The device loader calls the registry without knowing anything about
specific plugin implementations.

Build context
-------------
Every factory receives a ``BuildContext`` with all infrastructure objects
needed to construct a device or tariff:

    ctx.backends        — parsed BackendsConfig (host, port, tokens)
    ctx.ha_client       — HAClient (or None)
    ctx.modbus_pool     — shared ModbusConnectionPool, created on first use
    ctx.device_registry — populated DeviceRegistry (set in second pass,
                          available only for deferred factory types)
    ctx.data_dir        — writable directory for persistent plugin state
                          (e.g. fitted forecast models), or None

Device plugin metadata
----------------------
``register_device`` also declares what a plugin type can be used for, so the
loader can check the ``devices:`` section at startup:

``categories``  device categories the plugin can serve (empty = any).
``provides``    capabilities the plugin always provides natively, from its
                own settings (e.g. an openWB chargepoint has ``power_w``).
``bindable``    capabilities that can be bound in the device's
                ``capabilities:`` config (e.g. ``{entity: sensor.…}`` for
                Home Assistant backed plugins).

Deferred factories
------------------
Some devices (e.g. ``differential``) depend on other devices having been
built first.  Register them with ``deferred=True``; the loader collects them
in a second pass after the registry has been fully populated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BackendsConfig
from .device_model import Capability, DeviceCategory

_log = logging.getLogger(__name__)

# Factory type aliases:  (entity_id, cfg_dict, ctx) -> built object | None
DeviceFactory = Callable[[str, dict[str, Any], "BuildContext"], Any]
TariffFactory = Callable[[str, dict[str, Any], "BuildContext"], Any]
ForecastFactory = Callable[[str, dict[str, Any], "BuildContext"], Any]


@dataclass(frozen=True)
class DevicePluginInfo:
    """What a device plugin type can be used for (see module docstring)."""

    type_name: str
    factory: DeviceFactory
    deferred: bool = False
    categories: frozenset[DeviceCategory] = frozenset()
    provides: frozenset[Capability] = frozenset()
    bindable: frozenset[Capability] = frozenset()


@dataclass
class BuildContext:
    """Carries all infrastructure objects into plugin factory functions."""

    backends: BackendsConfig
    ha_client: Any = None        # HAClient | None
    modbus_pool: Any = None      # ModbusConnectionPool | None (created lazily)
    device_registry: Any = None  # DeviceRegistry | None  (deferred pass only)
    data_dir: Path | None = None  # writable directory for plugin state (or None)


class PluginRegistry:
    """Registry of device and tariff factory functions, keyed by type name."""

    def __init__(self) -> None:
        self._device: dict[str, DevicePluginInfo] = {}
        self._tariff: dict[str, TariffFactory] = {}
        self._forecast: dict[str, ForecastFactory] = {}

    def register_device(
        self,
        type_name: str,
        factory: DeviceFactory,
        *,
        deferred: bool = False,
        categories: frozenset[DeviceCategory] | set[DeviceCategory] = frozenset(),
        provides: frozenset[Capability] | set[Capability] = frozenset(),
        bindable: frozenset[Capability] | set[Capability] = frozenset(),
    ) -> None:
        """Register *factory* for devices with ``type: <type_name>``.

        Parameters
        ----------
        deferred:
            When ``True`` the factory will be called in the second pass,
            after all non-deferred devices have been built and added to
            ``BuildContext.device_registry``.
        categories, provides, bindable:
            Plugin metadata, see the module docstring.
        """
        self._device[type_name] = DevicePluginInfo(
            type_name=type_name,
            factory=factory,
            deferred=deferred,
            categories=frozenset(categories),
            provides=frozenset(provides),
            bindable=frozenset(bindable),
        )

    def device_info(self, type_name: str) -> DevicePluginInfo | None:
        """Metadata of the device plugin *type_name*, or None if unknown."""
        return self._device.get(type_name)

    def device_types(self) -> list[str]:
        """All registered device plugin type names."""
        return sorted(self._device)

    def register_tariff(self, type_name: str, factory: TariffFactory) -> None:
        """Register *factory* for tariffs with ``type: <type_name>``."""
        self._tariff[type_name] = factory

    def register_forecast(self, type_name: str, factory: ForecastFactory) -> None:
        """Register *factory* for forecast providers with ``type: <type_name>``."""
        self._forecast[type_name] = factory

    def is_deferred(self, type_name: str) -> bool:
        """Return ``True`` when the given device type requires a second pass."""
        entry = self._device.get(type_name)
        return entry.deferred if entry else False

    def build_device(
        self, device_id: str, cfg: dict[str, Any], ctx: BuildContext
    ) -> Any:
        type_name = cfg.get("type", "")
        entry = self._device.get(type_name)
        if entry is None:
            _log.warning(
                "No device factory registered for type %r — skipping %r",
                type_name, device_id,
            )
            return None
        return entry.factory(device_id, cfg, ctx)

    def build_tariff(
        self, tariff_id: str, cfg: dict[str, Any], ctx: BuildContext
    ) -> Any:
        type_name = cfg.get("type", "")
        factory = self._tariff.get(type_name)
        if factory is None:
            _log.warning(
                "No tariff factory registered for type %r — skipping %r",
                type_name, tariff_id,
            )
            return None
        return factory(tariff_id, cfg, ctx)

    def build_forecast(
        self, forecast_id: str, cfg: dict[str, Any], ctx: BuildContext
    ) -> Any:
        type_name = cfg.get("type", "")
        factory = self._forecast.get(type_name)
        if factory is None:
            _log.warning(
                "No forecast factory registered for type %r — skipping %r",
                type_name, forecast_id,
            )
            return None
        return factory(forecast_id, cfg, ctx)

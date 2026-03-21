"""PluginRegistry — maps type-name strings to factory callables.

All device and tariff plugin types register themselves here.
The device loader calls the registry without knowing anything about
specific plugin implementations.

Build context
-------------
Every factory receives a ``BuildContext`` with all infrastructure objects
needed to construct a device or tariff:

    ctx.backends        — parsed BackendsConfig (host, port, tokens)
    ctx.iobroker_pool   — shared IoBrokerConnectionPool (or None)
    ctx.ha_client       — HAClient (or None)
    ctx.device_registry — populated DeviceRegistry (set in second pass,
                          available only for deferred factory types)

Deferred factories
------------------
Some devices (e.g. ``differential``) depend on other devices having been
built first.  Register them with ``deferred=True``; the loader collects them
in a second pass after the registry has been fully populated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import BackendsConfig

_log = logging.getLogger(__name__)

# Factory type aliases:  (entity_id, cfg_dict, ctx) -> built object | None
DeviceFactory = Callable[[str, dict[str, Any], "BuildContext"], Any]
TariffFactory = Callable[[str, dict[str, Any], "BuildContext"], Any]


@dataclass
class BuildContext:
    """Carries all infrastructure objects into plugin factory functions."""

    backends: BackendsConfig
    iobroker_pool: Any = None    # IoBrokerConnectionPool | None
    ha_client: Any = None        # HAClient | None
    device_registry: Any = None  # DeviceRegistry | None  (deferred pass only)


class PluginRegistry:
    """Registry of device and tariff factory functions, keyed by type name."""

    def __init__(self) -> None:
        # value: (factory, is_deferred)
        self._device: dict[str, tuple[DeviceFactory, bool]] = {}
        self._tariff: dict[str, TariffFactory] = {}

    def register_device(
        self,
        type_name: str,
        factory: DeviceFactory,
        *,
        deferred: bool = False,
    ) -> None:
        """Register *factory* for devices with ``type: <type_name>``.

        Parameters
        ----------
        deferred:
            When ``True`` the factory will be called in the second pass,
            after all non-deferred devices have been built and added to
            ``BuildContext.device_registry``.
        """
        self._device[type_name] = (factory, deferred)

    def register_tariff(self, type_name: str, factory: TariffFactory) -> None:
        """Register *factory* for tariffs with ``type: <type_name>``."""
        self._tariff[type_name] = factory

    def is_deferred(self, type_name: str) -> bool:
        """Return ``True`` when the given device type requires a second pass."""
        entry = self._device.get(type_name)
        return entry[1] if entry else False

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
        factory, _ = entry
        return factory(device_id, cfg, ctx)

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

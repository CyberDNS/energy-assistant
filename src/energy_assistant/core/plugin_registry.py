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
``settings``    the plugin's own settings (host, profile, …) as
                ``ParamSpec``s.  ``None`` = not declared (anything goes);
                otherwise unknown, missing and ill-typed settings are config
                errors, and the config UI builds its form from them.
``binding``     the keys a capability binding of this plugin accepts
                (default: ``entity``).
``bindings``    per capability, when its binding accepts other keys than
                ``binding`` (e.g. ``import_entity`` only for power).

Tariff and forecast plugins declare their ``settings`` the same way
(``register_tariff`` / ``register_forecast``); ``core/config_schema`` checks
the ``tariffs:`` / ``forecasts:`` sections and the devices' ``forecast:``
against them.

Shared connections
------------------
Plugins that share one connection between devices (an MQTT bridge per
broker, an API client per account) keep it in a ``SharedPool``.  When the
configuration is reloaded at runtime, connections that are still used are
kept and the others are stopped (``begin_build`` / ``sweep``).

Deferred factories
------------------
Some devices (e.g. ``differential``) depend on other devices having been
built first.  Register them with ``deferred=True``; the loader collects them
in a second pass after the registry has been fully populated.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .config import BackendsConfig
from .device_model import Capability, DeviceCategory, ParamSpec, humanize, specs_to_dict

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
    settings: Mapping[str, ParamSpec] | None = None
    binding: Mapping[str, ParamSpec] = field(default_factory=dict)
    bindings: Mapping[Capability, Mapping[str, ParamSpec]] = field(default_factory=dict)
    title: str = ""
    """Display name (default: the type name, humanized)."""
    description: str = ""
    legacy: bool = False
    """Old name of another type: still accepted, not offered for new devices."""

    def binding_for(self, capability: Capability) -> Mapping[str, ParamSpec]:
        """The keys a binding of *capability* accepts."""
        return self.bindings.get(capability, self.binding)

    def to_dict(self) -> dict[str, Any]:
        """JSON form for the config UI (``GET /api/config/schema``)."""
        return {
            "type": self.type_name,
            "title": self.title or humanize(self.type_name),
            "description": self.description,
            "categories": sorted(c.value for c in self.categories),
            "provides": sorted(c.value for c in self.provides),
            "bindable": sorted(c.value for c in self.bindable),
            "settings": (
                None if self.settings is None
                else specs_to_dict(self.settings)
            ),
            "binding": specs_to_dict(self.binding),
            "bindings": {
                cap.value: specs_to_dict(specs)
                for cap, specs in sorted(self.bindings.items(), key=lambda i: i[0].value)
            },
            "legacy": self.legacy,
        }


@dataclass(frozen=True)
class ServicePluginInfo:
    """Metadata of a tariff or forecast plugin."""

    type_name: str
    settings: Mapping[str, ParamSpec] | None = None
    """``None`` = not declared (anything goes)."""
    description: str = ""
    scopes: tuple[str, ...] = ()
    title: str = ""
    validator: Callable[[Mapping[str, Any]], list[str]] | None = None
    """Checks of the whole entry beyond its single settings (e.g. overlapping
    time windows); returns the problems.  Only run when the settings are valid."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type_name,
            "title": self.title or humanize(self.type_name),
            "description": self.description,
            "scopes": list(self.scopes),
            "settings": (
                None if self.settings is None else specs_to_dict(self.settings)
            ),
        }


#: Binding keys of a plugin that binds capabilities to Home Assistant entities.
ENTITY_BINDING: Mapping[str, ParamSpec] = {
    "entity": ParamSpec(str, required=True, entity=True, label="Entity",
                        description="Home Assistant entity that provides it"),
}


class SharedPool[K: Hashable, V]:
    """Connections shared between devices, keyed by their connection parameters.

    ``get`` returns the existing connection for *key* or creates it.  Around a
    rebuild of all devices call ``begin_build()`` first and ``sweep()``
    afterwards: every connection not asked for in between is stopped.
    """

    _pools: ClassVar[list[SharedPool[Any, Any]]] = []

    def __init__(self, stop: Callable[[V], Awaitable[None]]) -> None:
        self._items: dict[K, V] = {}
        self._used: set[K] = set()
        self._stop = stop
        SharedPool._pools.append(self)

    def get(self, key: K, factory: Callable[[], V]) -> V:
        self._used.add(key)
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = factory()
        return item

    @classmethod
    def begin_build(cls) -> None:
        for pool in cls._pools:
            pool._used.clear()

    @classmethod
    async def sweep(cls) -> None:
        for pool in cls._pools:
            for key in [k for k in pool._items if k not in pool._used]:
                item = pool._items.pop(key)
                try:
                    await pool._stop(item)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("Could not stop unused connection %r: %s", key, exc)
@dataclass
class BuildContext:
    """Carries all infrastructure objects into plugin factory functions."""

    backends: BackendsConfig
    ha_client: Any = None        # HAClient | None
    modbus_pool: Any = None      # ModbusConnectionPool | None (created lazily)
    device_registry: Any = None  # DeviceRegistry | None  (deferred pass only)
    data_dir: Path | None = None  # writable directory for plugin state (or None)
    tariffs: dict[str, Any] = field(default_factory=dict)
    """Tariffs built so far (a tariff built on another one finds it here)."""


class PluginRegistry:
    """Registry of device and tariff factory functions, keyed by type name."""

    def __init__(self) -> None:
        self._device: dict[str, DevicePluginInfo] = {}
        self._tariff: dict[str, TariffFactory] = {}
        self._forecast: dict[str, ForecastFactory] = {}
        self._tariff_info: dict[str, ServicePluginInfo] = {}
        self._forecast_info: dict[str, ServicePluginInfo] = {}

    def register_device(
        self,
        type_name: str,
        factory: DeviceFactory,
        *,
        deferred: bool = False,
        categories: frozenset[DeviceCategory] | set[DeviceCategory] = frozenset(),
        provides: frozenset[Capability] | set[Capability] = frozenset(),
        bindable: frozenset[Capability] | set[Capability] = frozenset(),
        settings: Mapping[str, ParamSpec] | None = None,
        binding: Mapping[str, ParamSpec] | None = None,
        bindings: Mapping[Capability, Mapping[str, ParamSpec]] | None = None,
        title: str = "",
        description: str = "",
        legacy: bool = False,
    ) -> None:
        """Register *factory* for devices with ``type: <type_name>``.

        Parameters
        ----------
        deferred:
            When ``True`` the factory will be called in the second pass,
            after all non-deferred devices have been built and added to
            ``BuildContext.device_registry``.
        categories, provides, bindable, settings, binding, bindings:
            Plugin metadata, see the module docstring.
        legacy:
            An old type name kept for existing configs.
        """
        self._device[type_name] = DevicePluginInfo(
            type_name=type_name,
            factory=factory,
            deferred=deferred,
            categories=frozenset(categories),
            provides=frozenset(provides),
            bindable=frozenset(bindable),
            settings=settings,
            binding=dict(binding) if binding is not None else (
                dict(ENTITY_BINDING) if bindable else {}
            ),
            bindings={cap: dict(specs) for cap, specs in (bindings or {}).items()},
            title=title,
            description=description,
            legacy=legacy,
        )

    def device_infos(self) -> list[DevicePluginInfo]:
        """Metadata of every device plugin, sorted by type name."""
        return [self._device[name] for name in sorted(self._device)]

    def device_info(self, type_name: str) -> DevicePluginInfo | None:
        """Metadata of the device plugin *type_name*, or None if unknown."""
        return self._device.get(type_name)

    def device_types(self) -> list[str]:
        """All registered device plugin type names."""
        return sorted(self._device)

    def register_tariff(
        self,
        type_name: str,
        factory: TariffFactory,
        *,
        settings: Mapping[str, ParamSpec] | None = None,
        description: str = "",
        title: str = "",
        validator: Callable[[Mapping[str, Any]], list[str]] | None = None,
    ) -> None:
        """Register *factory* for tariffs with ``type: <type_name>``."""
        self._tariff[type_name] = factory
        self._tariff_info[type_name] = ServicePluginInfo(
            type_name, settings, description, title=title, validator=validator
        )

    def register_forecast(
        self,
        type_name: str,
        factory: ForecastFactory,
        *,
        settings: Mapping[str, ParamSpec] | None = None,
        description: str = "",
        scopes: tuple[str, ...] = ("system", "device"),
        title: str = "",
    ) -> None:
        """Register *factory* for forecast providers with ``type: <type_name>``.

        *scopes*: where it can be used — ``"system"`` (the top-level
        ``forecasts:`` section, e.g. PV generation) and / or ``"device"``
        (a consumer's ``forecast:``).
        """
        self._forecast[type_name] = factory
        self._forecast_info[type_name] = ServicePluginInfo(
            type_name, settings, description, tuple(scopes), title
        )

    def tariff_infos(self) -> list[ServicePluginInfo]:
        return [self._tariff_info[name] for name in sorted(self._tariff_info)]

    def forecast_infos(self) -> list[ServicePluginInfo]:
        return [self._forecast_info[name] for name in sorted(self._forecast_info)]

    def tariff_info(self, type_name: str) -> ServicePluginInfo | None:
        return self._tariff_info.get(type_name)

    def forecast_info(self, type_name: str) -> ServicePluginInfo | None:
        return self._forecast_info.get(type_name)

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

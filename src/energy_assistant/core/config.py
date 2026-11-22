"""ConfigManager protocol and structured AppConfig data classes.

Phase 1 — YAML config
----------------------
``AppConfig`` is the fully-parsed representation of the 6-section YAML file:

    backends:    connection parameters for Home Assistant and the MQTT broker
    tariffs:     pricing models, keyed by name
    devices:     device declarations (``DeviceSpec``), keyed by device_id;
                 their ``metered_by`` connections form the metering tree
    optimizer:   algorithm, horizon, schedule

``devices`` is parsed into typed ``DeviceSpec`` objects and validated against
the category schemas (``core/device_model``).  The ``tariffs``
and ``optimizer`` sections are kept as raw dicts so each plugin can validate
its own slice.

Editing at runtime
------------------
``config.yaml`` stays the single source of truth.  The configuration UI edits
its device entries in place (``config/store``) and the application applies
the file without a restart (``server.Application.reload_config``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .device_model import DeviceSpec
from .models import ConfigEntry

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ConfigManager(Protocol):
    """Manages device/plugin configuration entries."""

    async def load_entries(self) -> list[ConfigEntry]:
        """Return all declared device entries."""
        ...

    async def save_entry(self, entry: ConfigEntry) -> None:
        """Persist a new or updated entry."""
        ...

    async def delete_entry(self, entry_id: str) -> None:
        """Remove the entry with *entry_id*."""
        ...


# ---------------------------------------------------------------------------
# Structured config data classes
# ---------------------------------------------------------------------------


@dataclass
class HomeAssistantConfig:
    """Connection parameters for the Home Assistant REST API."""

    url: str
    token: str
    timeout_s: float = 10.0


@dataclass
class MqttConfig:
    """Connection parameters for an MQTT broker."""

    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None


@dataclass
class BackendsConfig:
    """Connection parameters for all configured backends."""

    homeassistant: HomeAssistantConfig | None = None
    mqtt: MqttConfig | None = None


@dataclass
class AppConfig:
    """Fully-parsed application configuration from the YAML file.

    ``devices`` holds validated ``DeviceSpec`` objects.  The raw dicts in
    ``tariffs`` and ``optimizer`` are validated by the relevant
    plugins / loaders — not here.
    """

    backends: BackendsConfig = field(default_factory=BackendsConfig)
    tariffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    devices: dict[str, DeviceSpec] = field(default_factory=dict)
    forecasts: dict[str, dict[str, Any]] = field(default_factory=dict)
    optimizer: dict[str, Any] = field(default_factory=dict)
    controller: dict[str, Any] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)
    logging: dict[str, Any] = field(default_factory=dict)
    energy_dashboard: dict[str, Any] = field(default_factory=dict)
    """Import of the Home Assistant Energy dashboard's consumers
    (``loader/energy_dashboard.py``)."""
    default_tariff_id: str | None = None
    """Tariff used for devices/loads that have no explicit ``tariff:`` key.

    Typically the main spot-price tariff (e.g. ``household`` / Tibber).
    Set by marking a tariff with ``default: true`` in config.yaml.
    """

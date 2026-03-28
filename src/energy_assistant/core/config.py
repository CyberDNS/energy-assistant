"""ConfigManager protocol and structured AppConfig data classes.

Phase 1 — YAML config
----------------------
``AppConfig`` is the fully-parsed representation of the 6-section YAML file:

    backends:    connection parameters for ioBroker and Home Assistant
    tariffs:     pricing models, keyed by name
    devices:     device declarations, keyed by device_id
    topology:    physical meter wiring as a tree
    assets:      managed objects (EVs, heat stores) with targets
    optimizer:   algorithm, horizon, schedule

The ``tariffs``, ``devices``, ``topology``, ``assets``, and ``optimizer``
sections are kept as raw dicts so each plugin can validate its own slice.

Phase 2 — JSON files (future)
------------------------------
Both phases implement the same ``ConfigManager`` protocol so nothing else
in the platform needs to change when moving from YAML to JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

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
class IoBrokerConfig:
    """Connection parameters for the ioBroker simple-api."""

    host: str
    port: int = 8087
    api_token: str | None = None
    timeout_s: float = 5.0


@dataclass
class HomeAssistantConfig:
    """Connection parameters for the Home Assistant REST API."""

    url: str
    token: str
    timeout_s: float = 10.0


@dataclass
class BackendsConfig:
    """Connection parameters for all configured backends."""

    iobroker: IoBrokerConfig | None = None
    homeassistant: HomeAssistantConfig | None = None


@dataclass
class AppConfig:
    """Fully-parsed application configuration from the YAML file.

    The raw dicts in ``tariffs``, ``devices``, ``topology``, ``assets``,
    and ``optimizer`` are validated by the relevant plugins / loaders —
    not here.
    """

    backends: BackendsConfig = field(default_factory=BackendsConfig)
    tariffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    forecasts: dict[str, dict[str, Any]] = field(default_factory=dict)
    topology: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, Any] = field(default_factory=dict)
    optimizer: dict[str, Any] = field(default_factory=dict)
    controller: dict[str, Any] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)
    default_tariff_id: str | None = None
    """Tariff used for devices/loads that have no explicit ``tariff:`` key.

    Typically the main spot-price tariff (e.g. ``household`` / Tibber).
    Set by marking a tariff with ``default: true`` in config.yaml.
    """

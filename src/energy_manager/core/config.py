"""
ConfigManager protocol.

Both the YAML-based (Phase 1) and JSON-based (Phase 2) implementations
satisfy this interface, so no other part of the platform needs to change
when switching between them.
"""

from __future__ import annotations

from typing import Protocol

from .models import ConfigEntry


class ConfigManager(Protocol):
    async def load_entries(self) -> list[ConfigEntry]:
        """Return all configured device/integration entries."""
        ...

    async def save_entry(self, entry: ConfigEntry) -> None:
        """Persist a new or updated entry."""
        ...

    async def delete_entry(self, entry_id: str) -> None:
        """Remove the entry with the given id."""
        ...

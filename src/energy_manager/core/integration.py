"""
Integration data model.

An *integration* is a config-driven virtual device that reads power measurements
from an arbitrary backend (ioBroker OID, Home Assistant entity, or a Jinja2
template that references other integrations).

Sign convention
---------------
``power_w`` follows the *meter* convention used throughout the platform:
positive = importing / consuming, negative = exporting / generating.
The raw value from the data source is used as-is — the loader does not flip signs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


@dataclass
class IntegrationState:
    """Snapshot of one named integration at a point in time."""

    name: str
    power_w: float | None = None
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@runtime_checkable
class IntegrationSourceProtocol(Protocol):
    """Any object that can produce an IntegrationState."""

    name: str

    async def read(self) -> IntegrationState:
        """Read current state from the underlying data source."""
        ...

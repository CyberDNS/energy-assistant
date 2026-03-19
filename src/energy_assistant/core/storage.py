"""StorageBackend protocol — persists time-series device measurements.

Runtime device state is always in-memory.  The StorageBackend is only
for persisted history (graphs, analytics, state across restarts).

Configuration is **never** stored in SQLite — only time-series data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import Measurement


class StorageBackend(Protocol):
    """Persists and retrieves time-series device measurements."""

    async def write(self, measurement: Measurement) -> None:
        """Persist a single measurement."""
        ...

    async def query(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Measurement]:
        """Return measurements for *device_id* in the time window [start, end]."""
        ...

"""
StorageBackend protocol.

The storage layer is only for persisted history (time-series data for graphs
and analytics).  Runtime device state is always held in memory.

Configuration is never stored here — only ``Measurement`` records.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import Measurement


class StorageBackend(Protocol):
    async def write(self, measurement: Measurement) -> None:
        """Persist a single measurement."""
        ...

    async def query(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Measurement]:
        """
        Return all measurements for *device_id* in the half-open interval
        [start, end], ordered by timestamp ascending.
        """
        ...

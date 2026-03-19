"""SqliteStorageBackend — persists time-series device measurements.

Uses ``aiosqlite`` for non-blocking I/O within the asyncio event loop.

Schema
------
A single ``measurements`` table with a composite primary key on
``(device_id, timestamp)``.  No external database server is required.

Lifecycle
---------
Call ``start()`` before the first write/query and ``stop()`` on shutdown::

    storage = SqliteStorageBackend("data/history.db")
    await storage.start()
    # ... run the application ...
    await storage.stop()
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from ..core.models import Measurement

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS measurements (
    device_id  TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    power_w    REAL,
    energy_kwh REAL,
    soc_pct    REAL,
    extra      TEXT,
    PRIMARY KEY (device_id, timestamp)
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_device_timestamp
    ON measurements (device_id, timestamp)
"""

_INSERT = """
INSERT OR REPLACE INTO measurements
    (device_id, timestamp, power_w, energy_kwh, soc_pct, extra)
VALUES (?, ?, ?, ?, ?, ?)
"""

_QUERY = """
SELECT device_id, timestamp, power_w, energy_kwh, soc_pct, extra
FROM measurements
WHERE device_id = ? AND timestamp BETWEEN ? AND ?
ORDER BY timestamp
"""


class SqliteStorageBackend:
    """SQLite-backed ``StorageBackend`` using ``aiosqlite``."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        """Open the database and ensure the schema is in place."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_INDEX)
        await self._db.commit()

    async def stop(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def write(self, measurement: Measurement) -> None:
        """Persist a single measurement.  Overwrites any existing entry for
        the same ``(device_id, timestamp)`` pair."""
        assert self._db is not None, "Call start() before write()"
        extra_json = json.dumps(measurement.extra) if measurement.extra else None
        await self._db.execute(
            _INSERT,
            (
                measurement.device_id,
                measurement.timestamp.isoformat(),
                measurement.power_w,
                measurement.energy_kwh,
                measurement.soc_pct,
                extra_json,
            ),
        )
        await self._db.commit()

    async def query(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Measurement]:
        """Return all measurements for *device_id* in ``[start, end]``."""
        assert self._db is not None, "Call start() before query()"
        async with self._db.execute(
            _QUERY,
            (device_id, start.isoformat(), end.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()

        result: list[Measurement] = []
        for row in rows:
            extra = json.loads(row[5]) if row[5] else {}
            result.append(
                Measurement(
                    device_id=row[0],
                    timestamp=datetime.fromisoformat(row[1]),
                    power_w=row[2],
                    energy_kwh=row[3],
                    soc_pct=row[4],
                    extra=extra,
                )
            )
        return result

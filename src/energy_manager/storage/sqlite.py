"""
SQLite-backed StorageBackend implementation.

Uses ``aiosqlite`` for non-blocking I/O.  No external database server is
required — a single file on disk is sufficient for a home installation.

The table schema has a composite primary key of ``(device_id, timestamp)``
and an explicit index on the same columns to make range queries fast.

Lifecycle
---------
    backend = SqliteStorageBackend("data/history.db")
    await backend.start()          # creates the DB file and schema if needed
    ...
    await backend.stop()           # closes the connection cleanly
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
    extra      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (device_id, timestamp)
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_device_timestamp
    ON measurements (device_id, timestamp)
"""

_INSERT = """
INSERT OR REPLACE INTO measurements (device_id, timestamp, power_w, energy_kwh, soc_pct, extra)
VALUES (?, ?, ?, ?, ?, ?)
"""

_QUERY = """
SELECT device_id, timestamp, power_w, energy_kwh, soc_pct, extra
FROM measurements
WHERE device_id = ? AND timestamp >= ? AND timestamp <= ?
ORDER BY timestamp ASC
"""


class SqliteStorageBackend:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        """Open the database connection and create schema if needed."""
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
        assert self._db is not None, "Call start() before write()"
        await self._db.execute(
            _INSERT,
            (
                measurement.device_id,
                measurement.timestamp.isoformat(),
                measurement.power_w,
                measurement.energy_kwh,
                measurement.soc_pct,
                json.dumps(measurement.extra),
            ),
        )
        await self._db.commit()

    async def query(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Measurement]:
        assert self._db is not None, "Call start() before query()"
        cursor = await self._db.execute(
            _QUERY,
            (device_id, start.isoformat(), end.isoformat()),
        )
        rows = await cursor.fetchall()
        return [
            Measurement(
                device_id=row[0],
                timestamp=datetime.fromisoformat(row[1]),
                power_w=row[2],
                energy_kwh=row[3],
                soc_pct=row[4],
                extra=json.loads(row[5]),
            )
            for row in rows
        ]

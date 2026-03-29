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
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import aiosqlite

from ..core.models import Measurement

_log = logging.getLogger(__name__)

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

_CREATE_LEDGER_TABLE = """
CREATE TABLE IF NOT EXISTS ledger_state (
    device_id       TEXT PRIMARY KEY,
    cost_basis      REAL NOT NULL,
    stored_energy_kwh REAL NOT NULL,
    updated_at      TEXT NOT NULL
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_device_timestamp
    ON measurements (device_id, timestamp)
"""

_UPSERT_LEDGER = """
INSERT INTO ledger_state (device_id, cost_basis, stored_energy_kwh, updated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(device_id) DO UPDATE SET
    cost_basis        = excluded.cost_basis,
    stored_energy_kwh = excluded.stored_energy_kwh,
    updated_at        = excluded.updated_at
"""

_LOAD_LEDGER = """
SELECT cost_basis, stored_energy_kwh FROM ledger_state WHERE device_id = ?
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

    @staticmethod
    def _ha_fallback_db_paths() -> list[Path]:
        """Return Home Assistant-safe fallback DB paths in preference order."""
        fallbacks: list[Path] = []
        if Path("/data").exists():
            fallbacks.append(Path("/data/energy-assistant.db"))
        if Path("/config").exists():
            # Useful when addon_config is mapped and users should inspect the DB.
            fallbacks.append(Path("/config/energy-assistant/energy-assistant.db"))
        return fallbacks

    @staticmethod
    async def _open_db(path: Path) -> aiosqlite.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        return await aiosqlite.connect(path)

    async def start(self) -> None:
        """Open the database and ensure the schema is in place."""
        try:
            self._db = await self._open_db(self._db_path)
        except sqlite3.OperationalError as exc:
            for fallback in self._ha_fallback_db_paths():
                if fallback == self._db_path:
                    continue
                _log.warning(
                    "Could not open SQLite DB at %s (%s); trying fallback %s",
                    self._db_path,
                    exc,
                    fallback,
                )
                try:
                    self._db_path = fallback
                    self._db = await self._open_db(self._db_path)
                    break
                except sqlite3.OperationalError:
                    continue
            else:
                raise

        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_LEDGER_TABLE)
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

    async def save_ledger_state(
        self,
        device_id: str,
        cost_basis: float,
        stored_energy_kwh: float,
    ) -> None:
        """Persist the current ledger state for *device_id* (upsert)."""
        assert self._db is not None, "Call start() before save_ledger_state()"
        from datetime import timezone
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            _UPSERT_LEDGER,
            (device_id, cost_basis, stored_energy_kwh, now),
        )
        await self._db.commit()

    async def load_ledger_state(
        self,
        device_id: str,
    ) -> tuple[float, float] | None:
        """Return ``(cost_basis, stored_energy_kwh)`` for *device_id*, or ``None``."""
        assert self._db is not None, "Call start() before load_ledger_state()"
        async with self._db.execute(_LOAD_LEDGER, (device_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return float(row[0]), float(row[1])

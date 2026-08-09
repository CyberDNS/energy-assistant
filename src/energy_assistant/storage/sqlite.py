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
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite

from ..assets.ev import EvDayOverride, EvWeeklyTarget
from ..core.models import Measurement, SignalPoint

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

_CREATE_LEDGER_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS ledger_history (
    device_id           TEXT NOT NULL,
    timestamp           TEXT NOT NULL,
    cost_basis          REAL NOT NULL,
    stored_energy_kwh   REAL NOT NULL,
    PRIMARY KEY (device_id, timestamp)
)
"""

_CREATE_LEDGER_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_ledger_history_device_ts
    ON ledger_history (device_id, timestamp)
"""

_INSERT_LEDGER_HISTORY = """
INSERT OR IGNORE INTO ledger_history (device_id, timestamp, cost_basis, stored_energy_kwh)
VALUES (?, ?, ?, ?)
"""

_QUERY_LEDGER_HISTORY = """
SELECT timestamp, cost_basis, stored_energy_kwh
FROM ledger_history
WHERE device_id = ? AND timestamp BETWEEN ? AND ?
ORDER BY timestamp
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_device_timestamp
    ON measurements (device_id, timestamp)
"""

_CREATE_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    value     REAL,
    PRIMARY KEY (signal_id, timestamp)
)
"""

_CREATE_SIGNALS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_signal_timestamp
    ON signals (signal_id, timestamp)
"""

_INSERT_SIGNAL = """
INSERT OR REPLACE INTO signals (signal_id, timestamp, value)
VALUES (?, ?, ?)
"""

_QUERY_SIGNAL = """
SELECT timestamp, value
FROM signals
WHERE signal_id = ? AND timestamp BETWEEN ? AND ?
ORDER BY timestamp
"""

_CREATE_EV_WEEKLY_PLAN_TABLE = """
CREATE TABLE IF NOT EXISTS ev_weekly_plan (
    asset_id       TEXT NOT NULL,
    weekday        INTEGER NOT NULL,
    enabled        INTEGER NOT NULL,
    target_soc_pct REAL NOT NULL,
    target_by      TEXT NOT NULL,
    PRIMARY KEY (asset_id, weekday)
)
"""

_UPSERT_EV_WEEKLY_ROW = """
INSERT INTO ev_weekly_plan (asset_id, weekday, enabled, target_soc_pct, target_by)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(asset_id, weekday) DO UPDATE SET
    enabled        = excluded.enabled,
    target_soc_pct = excluded.target_soc_pct,
    target_by      = excluded.target_by
"""

_LOAD_ALL_EV_WEEKLY = "SELECT asset_id, weekday, enabled, target_soc_pct, target_by FROM ev_weekly_plan"

_CREATE_EV_DAY_OVERRIDES_TABLE = """
CREATE TABLE IF NOT EXISTS ev_day_overrides (
    asset_id       TEXT NOT NULL,
    date           TEXT NOT NULL,
    skip           INTEGER NOT NULL DEFAULT 0,
    target_soc_pct REAL,
    target_by      TEXT,
    PRIMARY KEY (asset_id, date)
)
"""

_UPSERT_EV_DAY_OVERRIDE = """
INSERT INTO ev_day_overrides (asset_id, date, skip, target_soc_pct, target_by)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(asset_id, date) DO UPDATE SET
    skip           = excluded.skip,
    target_soc_pct = excluded.target_soc_pct,
    target_by      = excluded.target_by
"""

_DELETE_EV_DAY_OVERRIDE = "DELETE FROM ev_day_overrides WHERE asset_id = ? AND date = ?"

_PURGE_EV_DAY_OVERRIDES = "DELETE FROM ev_day_overrides WHERE asset_id = ? AND date < ?"

_LOAD_ALL_EV_DAY_OVERRIDES = "SELECT asset_id, date, skip, target_soc_pct, target_by FROM ev_day_overrides"

_CREATE_EV_FORCE_CHARGE_TABLE = """
CREATE TABLE IF NOT EXISTS ev_force_charge (
    asset_id       TEXT PRIMARY KEY,
    target_soc_pct REAL NOT NULL,
    created_at     TEXT NOT NULL
)
"""

_UPSERT_EV_FORCE_CHARGE = """
INSERT INTO ev_force_charge (asset_id, target_soc_pct, created_at)
VALUES (?, ?, ?)
ON CONFLICT(asset_id) DO UPDATE SET
    target_soc_pct = excluded.target_soc_pct,
    created_at     = excluded.created_at
"""

_DELETE_EV_FORCE_CHARGE = "DELETE FROM ev_force_charge WHERE asset_id = ?"

_LOAD_ALL_EV_FORCE_CHARGE = "SELECT asset_id, target_soc_pct FROM ev_force_charge"

_CREATE_EV_DISABLED_TABLE = """
CREATE TABLE IF NOT EXISTS ev_disabled (
    asset_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
)
"""

_UPSERT_EV_DISABLED = """
INSERT INTO ev_disabled (asset_id, created_at) VALUES (?, ?)
ON CONFLICT(asset_id) DO NOTHING
"""

_DELETE_EV_DISABLED = "DELETE FROM ev_disabled WHERE asset_id = ?"

_LOAD_ALL_EV_DISABLED = "SELECT asset_id FROM ev_disabled"

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
    async def _open_db(path: Path) -> aiosqlite.Connection:
        """Create parent directory and open the SQLite database."""
        path.parent.mkdir(parents=True, exist_ok=True)
        return await aiosqlite.connect(path)

    async def start(self) -> None:
        """Open the database and ensure the schema is in place."""
        try:
            self._db = await self._open_db(self._db_path)
        except (sqlite3.OperationalError, OSError) as exc:
            # In HA: /data is always mounted and writable by the Supervisor.
            # If we can't open the DB there, it's a configuration issue, not a fallback scenario.
            _log.error(
                "Could not open SQLite DB at %s (%s: %s). "
                "In Home Assistant, ensure /data is mounted and writable. "
                "In local mode, check that the parent directory exists and is writable. "
                "To use a custom DB path, set ENERGY_ASSISTANT_DB environment variable.",
                self._db_path,
                exc.__class__.__name__,
                exc,
            )
            raise

        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_LEDGER_TABLE)
        await self._db.execute(_CREATE_LEDGER_HISTORY_TABLE)
        await self._db.execute(_CREATE_EV_WEEKLY_PLAN_TABLE)
        await self._db.execute(_CREATE_EV_DAY_OVERRIDES_TABLE)
        await self._db.execute(_CREATE_EV_FORCE_CHARGE_TABLE)
        await self._db.execute(_CREATE_EV_DISABLED_TABLE)
        # Superseded by ev_weekly_plan + ev_day_overrides
        await self._db.execute("DROP TABLE IF EXISTS ev_targets")
        await self._db.execute(_CREATE_SIGNALS_TABLE)
        await self._db.execute(_CREATE_INDEX)
        await self._db.execute(_CREATE_LEDGER_HISTORY_INDEX)
        await self._db.execute(_CREATE_SIGNALS_INDEX)
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

    async def write_signal(
        self,
        signal_id: str,
        timestamp: datetime,
        value: float | None,
    ) -> None:
        """Persist a single environmental signal sample.  Overwrites any
        existing entry for the same ``(signal_id, timestamp)`` pair."""
        assert self._db is not None, "Call start() before write_signal()"
        await self._db.execute(_INSERT_SIGNAL, (signal_id, timestamp.isoformat(), value))
        await self._db.commit()

    async def query_signals(
        self,
        signal_id: str,
        start: datetime,
        end: datetime,
    ) -> list[SignalPoint]:
        """Return all samples for *signal_id* in ``[start, end]``."""
        assert self._db is not None, "Call start() before query_signals()"
        async with self._db.execute(
            _QUERY_SIGNAL,
            (signal_id, start.isoformat(), end.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            SignalPoint(timestamp=datetime.fromisoformat(row[0]), value=row[1])
            for row in rows
            if row[1] is not None
        ]

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

    async def append_ledger_history(
        self,
        device_id: str,
        cost_basis: float,
        stored_energy_kwh: float,
        timestamp: datetime | None = None,
    ) -> None:
        """Append a ledger snapshot to the history table.

        Uses INSERT OR IGNORE so duplicate (device_id, timestamp) pairs are
        silently dropped — prevents flooding when the control tick fires more
        frequently than desired.
        """
        assert self._db is not None, "Call start() before append_ledger_history()"
        from datetime import timezone
        ts = (timestamp or datetime.now(timezone.utc)).isoformat()
        await self._db.execute(
            _INSERT_LEDGER_HISTORY,
            (device_id, ts, cost_basis, stored_energy_kwh),
        )
        await self._db.commit()

    async def query_ledger_history(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """Return ledger history rows for *device_id* in ``[start, end]``."""
        assert self._db is not None, "Call start() before query_ledger_history()"
        async with self._db.execute(
            _QUERY_LEDGER_HISTORY,
            (device_id, start.isoformat(), end.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "t": row[0],
                "cost_basis_eur_per_kwh": row[1],
                "stored_energy_kwh": row[2],
            }
            for row in rows
        ]

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

    # ------------------------------------------------------------------
    # EV charge plans (weekly plan + dated overrides + force charge)
    # ------------------------------------------------------------------

    async def set_ev_weekly_plan(
        self,
        asset_id: str,
        entries: "list[EvWeeklyTarget]",
    ) -> None:
        """Replace the full weekly plan for *asset_id* (7 rows max)."""
        assert self._db is not None, "Call start() before set_ev_weekly_plan()"
        await self._db.execute("DELETE FROM ev_weekly_plan WHERE asset_id = ?", (asset_id,))
        for e in entries:
            await self._db.execute(
                _UPSERT_EV_WEEKLY_ROW,
                (asset_id, e.weekday, int(e.enabled), e.target_soc_pct, e.target_by),
            )
        await self._db.commit()

    async def load_all_ev_weekly_plans(self) -> "dict[str, dict[int, EvWeeklyTarget]]":
        assert self._db is not None, "Call start() before load_all_ev_weekly_plans()"
        async with self._db.execute(_LOAD_ALL_EV_WEEKLY) as cursor:
            rows = await cursor.fetchall()
        result: dict[str, dict[int, EvWeeklyTarget]] = {}
        for asset_id, weekday, enabled, soc, hhmm in rows:
            result.setdefault(asset_id, {})[int(weekday)] = EvWeeklyTarget(
                weekday=int(weekday),
                enabled=bool(enabled),
                target_soc_pct=float(soc),
                target_by=str(hhmm),
            )
        return result

    async def set_ev_day_override(self, asset_id: str, override: "EvDayOverride") -> None:
        assert self._db is not None, "Call start() before set_ev_day_override()"
        await self._db.execute(
            _UPSERT_EV_DAY_OVERRIDE,
            (
                asset_id,
                override.date.isoformat(),
                int(override.skip),
                override.target_soc_pct,
                override.target_by,
            ),
        )
        await self._db.commit()

    async def clear_ev_day_override(self, asset_id: str, day: date) -> None:
        assert self._db is not None, "Call start() before clear_ev_day_override()"
        await self._db.execute(_DELETE_EV_DAY_OVERRIDE, (asset_id, day.isoformat()))
        await self._db.commit()

    async def purge_ev_day_overrides_before(self, asset_id: str, day: date) -> None:
        """Delete expired overrides (dates strictly before *day*, asset-local today)."""
        assert self._db is not None, "Call start() before purge_ev_day_overrides_before()"
        await self._db.execute(_PURGE_EV_DAY_OVERRIDES, (asset_id, day.isoformat()))
        await self._db.commit()

    async def load_all_ev_day_overrides(self) -> "dict[str, dict[date, EvDayOverride]]":
        assert self._db is not None, "Call start() before load_all_ev_day_overrides()"
        async with self._db.execute(_LOAD_ALL_EV_DAY_OVERRIDES) as cursor:
            rows = await cursor.fetchall()
        result: dict[str, dict[date, EvDayOverride]] = {}
        for asset_id, date_str, skip, soc, hhmm in rows:
            d = date.fromisoformat(date_str)
            result.setdefault(asset_id, {})[d] = EvDayOverride(
                date=d,
                skip=bool(skip),
                target_soc_pct=float(soc) if soc is not None else None,
                target_by=str(hhmm) if hhmm is not None else None,
            )
        return result

    async def set_ev_force_charge(self, asset_id: str, target_soc_pct: float) -> None:
        assert self._db is not None, "Call start() before set_ev_force_charge()"
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(_UPSERT_EV_FORCE_CHARGE, (asset_id, target_soc_pct, now))
        await self._db.commit()

    async def clear_ev_force_charge(self, asset_id: str) -> None:
        assert self._db is not None, "Call start() before clear_ev_force_charge()"
        await self._db.execute(_DELETE_EV_FORCE_CHARGE, (asset_id,))
        await self._db.commit()

    async def load_all_ev_force_charge(self) -> dict[str, float]:
        assert self._db is not None, "Call start() before load_all_ev_force_charge()"
        async with self._db.execute(_LOAD_ALL_EV_FORCE_CHARGE) as cursor:
            rows = await cursor.fetchall()
        return {row[0]: float(row[1]) for row in rows}

    async def set_ev_disabled(self, asset_id: str) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(_UPSERT_EV_DISABLED, (asset_id, now))
        await self._db.commit()

    async def clear_ev_disabled(self, asset_id: str) -> None:
        assert self._db is not None
        await self._db.execute(_DELETE_EV_DISABLED, (asset_id,))
        await self._db.commit()

    async def load_all_ev_disabled(self) -> set[str]:
        assert self._db is not None
        async with self._db.execute(_LOAD_ALL_EV_DISABLED) as cursor:
            rows = await cursor.fetchall()
        return {row[0] for row in rows}

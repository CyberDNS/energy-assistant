"""SqliteStorageBackend — persists time-series device measurements.

Uses ``aiosqlite`` for non-blocking I/O within the asyncio event loop.

Schema
------
A single ``measurements`` table with a composite primary key on
``(device_id, timestamp)``.  No external database server is required.

Retention
---------
History tables are not self-limiting: the owner calls ``purge_before()``
periodically.  ``enable_incremental_vacuum()`` converts the DB once so that
purged pages are actually returned to the filesystem.

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
from ..core.models import Measurement
from ..core.savings import ActualSlot, PredictedSlot

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

# Redundant with the measurements primary key (same columns) — it only
# doubled the index size on disk.  Dropped by drop_redundant_index(), after
# the purge has shrunk it (dropping a multi-GB index is slow).
_DROP_REDUNDANT_INDEX = "DROP INDEX IF EXISTS idx_device_timestamp"

# Batched deletes: each batch is its own short transaction so other writes
# interleave and an interrupted purge keeps the progress it made.  Rows are
# inserted chronologically, so the oldest ones come first in rowid order and
# the LIMIT scan stops early.
_PURGE_BATCH_SQL = {
    table: f"""
DELETE FROM {table} WHERE rowid IN (
    SELECT rowid FROM {table} WHERE timestamp < ? LIMIT ?
)
"""
    for table in ("measurements", "ledger_history")
}

_PURGE_BATCH_SIZE = 10_000

# PRAGMA auto_vacuum value for INCREMENTAL mode.
_AUTO_VACUUM_INCREMENTAL = 2

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

# Savings accounting (core/savings.py).  One row per accounting slot; these
# tables are tiny (96 rows a day) and are never purged — the KPI totals are
# sums over them.
_CREATE_SAVINGS_ACTUAL_TABLE = """
CREATE TABLE IF NOT EXISTS savings_actual (
    slot_start      TEXT PRIMARY KEY,
    hours           REAL NOT NULL,
    baseline_cost   REAL NOT NULL,
    actual_cost     REAL NOT NULL,
    inventory_delta REAL NOT NULL
)
"""

# kind = 'day_ahead' (frozen once, never overwritten) or 'latest' (the last
# plan made before the slot began).
_CREATE_SAVINGS_PREDICTED_TABLE = """
CREATE TABLE IF NOT EXISTS savings_predicted (
    slot_start      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    hours           REAL NOT NULL,
    baseline_cost   REAL NOT NULL,
    plan_cost       REAL NOT NULL,
    inventory_delta REAL NOT NULL,
    PRIMARY KEY (slot_start, kind)
)
"""

_CREATE_SAVINGS_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS savings_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at TEXT NOT NULL,
    state      TEXT NOT NULL
)
"""

# Only slots that have both a measurement and a day-ahead prediction are
# compared, so the three KPIs always add up (deviation = real − predicted).
# A prediction covers a whole slot; it is scaled to the part of the slot
# that was actually measured.
_QUERY_SAVINGS = """
SELECT a.slot_start,
       a.hours,
       a.baseline_cost,
       a.actual_cost,
       a.baseline_cost - a.actual_cost + a.inventory_delta,
       (d.baseline_cost - d.plan_cost + d.inventory_delta) * a.hours / d.hours,
       (l.baseline_cost - l.plan_cost + l.inventory_delta) * a.hours / l.hours,
       a.inventory_delta,
       d.created_at, d.hours, d.baseline_cost, d.plan_cost, d.inventory_delta,
       l.created_at, l.hours, l.baseline_cost, l.plan_cost, l.inventory_delta
FROM savings_actual a
JOIN savings_predicted d
  ON d.slot_start = a.slot_start AND d.kind = 'day_ahead' AND d.hours > 0
LEFT JOIN savings_predicted l
  ON l.slot_start = a.slot_start AND l.kind = 'latest' AND l.hours > 0
WHERE a.slot_start >= ? AND a.slot_start < ?
ORDER BY a.slot_start
"""

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
        await self._db.execute(_CREATE_SAVINGS_ACTUAL_TABLE)
        await self._db.execute(_CREATE_SAVINGS_PREDICTED_TABLE)
        await self._db.execute(_CREATE_SAVINGS_STATE_TABLE)
        # Superseded by ev_weekly_plan + ev_day_overrides
        await self._db.execute("DROP TABLE IF EXISTS ev_targets")
        await self._db.execute(_CREATE_LEDGER_HISTORY_INDEX)
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

    async def purge_before(
        self,
        cutoff: datetime,
        batch_size: int = _PURGE_BATCH_SIZE,
    ) -> int:
        """Delete measurements and ledger history older than *cutoff*.

        Deletes in committed batches of *batch_size* rows (see
        ``_PURGE_BATCH_SQL``).  Freed pages are returned to the filesystem via
        ``incremental_vacuum`` (a no-op until ``enable_incremental_vacuum()``
        has converted the DB).  Returns the number of rows deleted.
        """
        assert self._db is not None, "Call start() before purge_before()"
        ts = cutoff.isoformat()
        deleted = 0
        next_log = 1_000_000
        for sql in _PURGE_BATCH_SQL.values():
            while True:
                async with self._db.execute(sql, (ts, batch_size)) as cursor:
                    n = max(cursor.rowcount, 0)
                await self._db.commit()
                deleted += n
                if deleted >= next_log:
                    _log.info("History purge: %d rows deleted so far", deleted)
                    next_log += 1_000_000
                if n < batch_size:
                    break
        # incremental_vacuum frees one page per step — the result rows must
        # be consumed or only the first page is released.
        async with self._db.execute("PRAGMA incremental_vacuum") as cursor:
            await cursor.fetchall()
        await self._db.commit()
        return deleted

    async def drop_redundant_index(self) -> None:
        """Drop the obsolete ``idx_device_timestamp`` index if present."""
        assert self._db is not None, "Call start() before drop_redundant_index()"
        await self._db.execute(_DROP_REDUNDANT_INDEX)
        await self._db.commit()

    async def enable_incremental_vacuum(self) -> bool:
        """Switch the DB to ``auto_vacuum=INCREMENTAL``, rebuilding it once.

        Changing ``auto_vacuum`` on an existing DB only takes effect after a
        full ``VACUUM``, which also shrinks the file to its live data.  Runs
        only when the mode is not already set, so the rebuild happens once.
        Returns True if the VACUUM was performed.
        """
        assert self._db is not None, "Call start() before enable_incremental_vacuum()"
        async with self._db.execute("PRAGMA auto_vacuum") as cursor:
            row = await cursor.fetchone()
        if row is not None and int(row[0]) == _AUTO_VACUUM_INCREMENTAL:
            return False
        await self._db.execute(f"PRAGMA auto_vacuum = {_AUTO_VACUUM_INCREMENTAL}")
        # The connection is shared with the polling writes: one may open an
        # implicit transaction between our commit and the VACUUM, which
        # SQLite rejects — commit and retry.
        for attempt in range(5):
            await self._db.commit()
            try:
                await self._db.execute("VACUUM")
                return True
            except sqlite3.OperationalError as exc:
                if "transaction" not in str(exc) or attempt == 4:
                    raise
        return True

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

    # ------------------------------------------------------------------
    # Savings accounting
    # ------------------------------------------------------------------

    async def save_savings_actual(self, slot: ActualSlot) -> None:
        """Persist the measured result of one accounting slot."""
        assert self._db is not None, "Call start() before save_savings_actual()"
        await self._db.execute(
            "INSERT OR REPLACE INTO savings_actual VALUES (?, ?, ?, ?, ?)",
            (
                _utc_iso(slot.start), slot.hours,
                slot.baseline_cost, slot.actual_cost, slot.inventory_delta,
            ),
        )
        await self._db.commit()

    async def save_savings_predictions(
        self,
        kind: str,
        slots: "list[PredictedSlot]",
        created_at: datetime,
        overwrite: bool,
    ) -> None:
        """Persist predicted slots of *kind*.

        With ``overwrite=False`` a slot that already has a prediction of this
        kind keeps it — that is what freezes a day-ahead prediction.
        """
        assert self._db is not None, "Call start() before save_savings_predictions()"
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        await self._db.executemany(
            f"{verb} INTO savings_predicted VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    _utc_iso(s.start), kind, _utc_iso(created_at), s.hours,
                    s.baseline_cost, s.plan_cost, s.inventory_delta,
                )
                for s in slots
            ],
        )
        await self._db.commit()

    async def query_savings(self, start: datetime, end: datetime) -> list[dict]:
        """Compared slots in ``[start, end)``, oldest first.

        ``predicted`` is the day-ahead prediction, ``predicted_latest`` the
        one from the last plan before the slot (``None`` if there was none).
        """
        assert self._db is not None, "Call start() before query_savings()"
        async with self._db.execute(
            _QUERY_SAVINGS, (_utc_iso(start), _utc_iso(end))
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "slot_start": datetime.fromisoformat(row[0]),
                "hours": row[1],
                "baseline_cost": row[2],
                "actual_cost": row[3],
                "real": row[4],
                "predicted": row[5],
                "predicted_latest": row[6],
                "inventory_delta": row[7],
                "day_ahead": _prediction(row[8:13]),
                "latest": _prediction(row[13:18]),
            }
            for row in rows
        ]

    async def save_savings_state(self, state: dict) -> None:
        """Persist the shadow-site state (single row)."""
        assert self._db is not None, "Call start() before save_savings_state()"
        await self._db.execute(
            "INSERT OR REPLACE INTO savings_state VALUES (1, ?, ?)",
            (_utc_iso(datetime.now(timezone.utc)), json.dumps(state)),
        )
        await self._db.commit()

    async def load_savings_state(self) -> tuple[datetime, dict] | None:
        """Return ``(saved_at, state)``, or ``None`` if nothing was saved."""
        assert self._db is not None, "Call start() before load_savings_state()"
        async with self._db.execute(
            "SELECT updated_at, state FROM savings_state WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row[0]), json.loads(row[1])


def _prediction(cols: tuple) -> dict | None:
    """Raw components of one stored prediction (``None`` if there is none).

    Unlike ``predicted``/``predicted_latest`` these are not scaled to the
    measured hours of the slot.
    """
    created_at, hours, baseline_cost, plan_cost, inventory_delta = cols
    if created_at is None:
        return None
    return {
        "created_at": created_at,
        "hours": hours,
        "baseline_cost": baseline_cost,
        "plan_cost": plan_cost,
        "inventory_delta": inventory_delta,
        "saving": baseline_cost - plan_cost + inventory_delta,
    }


def _utc_iso(ts: datetime) -> str:
    """ISO string in UTC — one fixed format, so text comparison orders by time."""
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")

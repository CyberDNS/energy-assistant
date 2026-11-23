"""SqliteStorageBackend — persists time-series device measurements.

Uses ``aiosqlite`` for non-blocking I/O within the asyncio event loop.

Schema
------
Device history is kept in two compact tiers, both keyed on
``(device_id, timestamp)`` with the timestamp of the bucket's start:

``history_5m``
    One row per device and 5 minutes: mean power (split into its positive and
    negative part, so import and export do not cancel out), the last state of
    charge and the number of readings.  Kept ``HISTORY_5M_RETENTION``.
``history_1h``
    The same per hour, rolled up from the 5-minute rows.  Kept for good:
    24 rows per device and day.

Readings arrive every few seconds; ``write()`` only adds them to the open
bucket in memory and stores the bucket about once a minute.  Databases from
before the tiers hold a ``measurements`` table with every reading; it is
rolled up and removed in the background (``migrate_raw_measurements``).

Timestamps
----------
Timestamps are stored as ISO-8601 text and compared as text (range queries,
ordering, purging).  That only orders by time if every value has the same
offset, so all timestamps are converted to UTC before they are written or
used as a query bound — whatever offset the source delivered.  Naive
datetimes are taken to be UTC.

Retention
---------
History tables are not self-limiting: the owner calls ``purge_before()``
and ``maintain_history()`` periodically.  ``enable_incremental_vacuum()`` converts the DB once so that
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
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from ..assets.ev import EvDayOverride, EvWeeklyTarget
from ..core.execution_log import ExecutionSlot
from ..core.models import EnergyPlan, Measurement
from ..core.savings import SavingsSlot, SlotCorrection

_log = logging.getLogger(__name__)

HISTORY_5M_RETENTION = timedelta(days=15)
"""How long 5-minute rows are kept (the battery efficiency learning looks
back 14 days); the hourly rows stay."""

_BUCKET = timedelta(minutes=5)
_FLUSH_INTERVAL_S = 60.0
"""How often the open bucket of a device is stored, so charts and a restart
lose at most this much."""

_HISTORY_COLUMNS = """
    device_id  TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    pos_w      REAL,
    neg_w      REAL,
    soc_pct    REAL,
    samples    INTEGER NOT NULL,
    PRIMARY KEY (device_id, timestamp)
"""
_CREATE_HISTORY_TABLES = tuple(
    f"CREATE TABLE IF NOT EXISTS {table} ({_HISTORY_COLUMNS}) WITHOUT ROWID"
    for table in ("history_5m", "history_1h")
)

_UPSERT_HISTORY_5M = """
INSERT OR REPLACE INTO history_5m (device_id, timestamp, pos_w, neg_w, soc_pct, samples)
VALUES (?, ?, ?, ?, ?, ?)
"""

# Raw readings of one time window as 5-minute rows.  The single ``max()``
# makes SQLite take the bare ``soc_pct`` from the last reading of each bucket.
_ROLL_UP_RAW = """
INSERT OR IGNORE INTO history_5m (device_id, timestamp, pos_w, neg_w, soc_pct, samples)
SELECT device_id, bucket, pos_w, neg_w, soc_pct, samples FROM (
    SELECT device_id,
           substr(timestamp, 1, 14) || printf('%02d', CAST(substr(timestamp, 15, 2) AS INTEGER) / 5 * 5)
               || ':00+00:00' AS bucket,
           avg(max(power_w, 0)) AS pos_w,
           avg(max(-power_w, 0)) AS neg_w,
           soc_pct,
           count(power_w) AS samples,
           max(timestamp) AS last
    FROM measurements
    WHERE timestamp >= ? AND timestamp < ?
    GROUP BY device_id, bucket
)
"""

# 5-minute rows of whole hours as hourly rows, weighted by their readings.
_ROLL_UP_HOURS = """
INSERT OR REPLACE INTO history_1h (device_id, timestamp, pos_w, neg_w, soc_pct, samples)
SELECT device_id, hour, pos_w, neg_w, soc_pct, samples FROM (
    SELECT device_id,
           substr(timestamp, 1, 13) || ':00:00+00:00' AS hour,
           sum(pos_w * samples) / sum(CASE WHEN pos_w IS NOT NULL THEN samples END) AS pos_w,
           sum(neg_w * samples) / sum(CASE WHEN neg_w IS NOT NULL THEN samples END) AS neg_w,
           soc_pct,
           sum(samples) AS samples,
           max(timestamp) AS last
    FROM history_5m
    WHERE timestamp >= ? AND timestamp < ?
    GROUP BY device_id, hour
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
# "measurements" only exists in databases from before the history tiers.

_PURGE_BATCH_SIZE = 10_000

# PRAGMA user_version once the history tables hold only UTC timestamps
# (see _migrate_utc_timestamps).  Older versions stored the offset the source
# delivered, e.g. Tibber's local "+01:00".
_SCHEMA_VERSION_UTC_TIMESTAMPS = 1
_UTC_TIMESTAMP_TABLES = ("measurements", "ledger_history")

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

# Savings accounting (core/savings.py): real house vs. simulated uncontrolled
# house, one row per accounting slot (96 a day), kept for SAVINGS_KEEP_DAYS.
_CREATE_SAVINGS_SLOTS_TABLE = """
CREATE TABLE IF NOT EXISTS savings_slots (
    slot_start        TEXT PRIMARY KEY,
    hours             REAL NOT NULL,
    real_cost         REAL NOT NULL,
    uncontrolled_cost REAL NOT NULL,
    real_bill         REAL NOT NULL,
    uncontrolled_bill REAL NOT NULL,
    ev_saving         REAL NOT NULL
)
"""

# Per-slot detail added later (created on older DBs by start()): what the
# batteries and cars did in both houses, for the short-range charts.
_SAVINGS_SLOTS_ADDED_COLUMNS = {
    "real_battery_kwh": "REAL",
    "uncontrolled_battery_kwh": "REAL",
    "real_ev_kwh": "REAL",
    "uncontrolled_ev_kwh": "REAL",
    "real_stored_kwh": "REAL",
    "uncontrolled_stored_kwh": "REAL",
    "import_price": "REAL",
    "stored_detail": "TEXT",
    # Saving per component ({"batteries": €, "<car>": €, "<device>": €}) and
    # per-car / per-device detail ({"evs": {...}, "devices": {...}}).
    "components": "TEXT",
    "detail": "TEXT",
}

SAVINGS_KEEP_DAYS = 400
"""Savings slots are kept this long (the dashboard's longest range is 1 y)."""

_CREATE_SAVINGS_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS savings_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at TEXT NOT NULL,
    state      TEXT NOT NULL
)
"""

# Plan fidelity diagnostics, purged with the history (see purge_before).
# Every published plan, zlib-compressed JSON (~6 kB); day_ahead = 1 marks the
# plan that froze a day's savings prediction.
_CREATE_PLAN_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS plan_snapshots (
    created_at TEXT PRIMARY KEY,
    day_ahead  INTEGER NOT NULL DEFAULT 0,
    plan       BLOB NOT NULL
)
"""

# What the control loop did with the plan: one row per slot and device
# (see core/execution_log.py).
_CREATE_CONTROL_EXECUTION_TABLE = """
CREATE TABLE IF NOT EXISTS control_execution (
    slot_start      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    seconds         REAL NOT NULL,
    mode            TEXT NOT NULL,
    planned_w       REAL,
    setpoint_w      REAL,
    measured_w      REAL,
    plan_created_at TEXT,
    PRIMARY KEY (slot_start, device_id)
)
"""

_PURGE_SAVINGS_SQL = (
    ("DELETE FROM savings_slots WHERE slot_start < ?", SAVINGS_KEEP_DAYS),
)

_PURGE_DIAGNOSTICS_SQL = (
    "DELETE FROM plan_snapshots WHERE created_at < ?",
    "DELETE FROM control_execution WHERE slot_start < ?",
)

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

_CREATE_VEHICLE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS vehicle_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    charger_id  TEXT NOT NULL,
    vehicle_id  TEXT,              -- NULL = guest
    plugged_at  TEXT NOT NULL,     -- UTC ISO-8601, start of the plug-in
    started_at  TEXT NOT NULL,     -- UTC ISO-8601, start of this assignment
    ended_at    TEXT,              -- NULL while open
    source      TEXT NOT NULL      -- default | manual
)
"""

_CREATE_CHARGER_NEXT_VEHICLE_TABLE = """
CREATE TABLE IF NOT EXISTS charger_next_vehicle (
    charger_id TEXT PRIMARY KEY,
    choice     TEXT NOT NULL       -- vehicle id or "guest"
)
"""

_CREATE_CHARGER_GUEST_MODE_TABLE = """
CREATE TABLE IF NOT EXISTS charger_guest_mode (
    charger_id TEXT PRIMARY KEY,
    mode       TEXT NOT NULL       -- pv_only | instant
)
"""

# Tables whose rows belong to a vehicle (keyed by its former asset id).
_EV_ID_TABLES = ("ev_weekly_plan", "ev_day_overrides", "ev_force_charge", "ev_disabled")

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


@dataclass(frozen=True)
class HistoryRow:
    """One bucket of a device's history (5 minutes or an hour)."""

    timestamp: datetime  # start of the bucket, UTC
    pos_w: float | None  # mean of the positive part of the power
    neg_w: float | None  # mean of the negative part, as a positive number
    soc_pct: float | None  # last reading in the bucket

    @property
    def power_w(self) -> float | None:
        """Mean power over the bucket."""
        if self.pos_w is None and self.neg_w is None:
            return None
        return (self.pos_w or 0.0) - (self.neg_w or 0.0)


@dataclass
class _Bucket:
    """The readings of one device in the 5 minutes from ``start``."""

    start: datetime
    stored_at: float  # time.monotonic() of the last store
    pos_sum: float = 0.0
    neg_sum: float = 0.0
    samples: int = 0  # readings with a power value
    soc: float | None = None
    dirty: bool = False

    def add(self, power_w: float | None, soc_pct: float | None) -> None:
        if power_w is not None:
            self.pos_sum += max(power_w, 0.0)
            self.neg_sum += max(-power_w, 0.0)
            self.samples += 1
            self.dirty = True
        if soc_pct is not None:
            self.soc = soc_pct
            self.dirty = True


def _bucket_start(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


class SqliteStorageBackend:
    """SQLite-backed ``StorageBackend`` using ``aiosqlite``."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        # Open 5-minute bucket per device (see write()).
        self._buckets: dict[str, _Bucket] = {}
        # Last reading's timestamp per device: a device that reports slowly
        # is polled many times with the same reading.
        self._last_seen: dict[str, datetime] = {}
        self._has_raw = False

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

        for ddl in _CREATE_HISTORY_TABLES:
            await self._db.execute(ddl)
        self._has_raw = await self._table_exists("measurements")
        await self._db.execute(_CREATE_LEDGER_TABLE)
        await self._db.execute(_CREATE_LEDGER_HISTORY_TABLE)
        await self._db.execute(_CREATE_EV_WEEKLY_PLAN_TABLE)
        await self._db.execute(_CREATE_EV_DAY_OVERRIDES_TABLE)
        await self._db.execute(_CREATE_EV_FORCE_CHARGE_TABLE)
        await self._db.execute(_CREATE_EV_DISABLED_TABLE)
        await self._db.execute(_CREATE_VEHICLE_SESSIONS_TABLE)
        await self._db.execute(_CREATE_CHARGER_NEXT_VEHICLE_TABLE)
        await self._db.execute(_CREATE_CHARGER_GUEST_MODE_TABLE)
        # The accounting before #82; its rows were only kept for debugging.
        await self._db.execute("DROP TABLE IF EXISTS savings_actual")
        await self._db.execute("DROP TABLE IF EXISTS savings_predicted")
        await self._db.execute(_CREATE_SAVINGS_STATE_TABLE)
        await self._db.execute(_CREATE_SAVINGS_SLOTS_TABLE)
        await self._add_missing_columns("savings_slots", _SAVINGS_SLOTS_ADDED_COLUMNS)
        await self._db.execute(_CREATE_PLAN_SNAPSHOTS_TABLE)
        await self._db.execute(_CREATE_CONTROL_EXECUTION_TABLE)
        # Superseded by ev_weekly_plan + ev_day_overrides
        await self._db.execute("DROP TABLE IF EXISTS ev_targets")
        await self._db.execute(_CREATE_LEDGER_HISTORY_INDEX)
        await self._db.commit()
        await self._migrate_utc_timestamps()

    async def _migrate_utc_timestamps(self) -> None:
        """Rewrite history timestamps stored with a non-UTC offset, once.

        Runs only while ``PRAGMA user_version`` is below
        ``_SCHEMA_VERSION_UTC_TIMESTAMPS``.  A row whose UTC instant already
        exists for the same device replaces that row (same instant, same
        device — they are duplicates).
        """
        assert self._db is not None
        async with self._db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        if row is not None and int(row[0]) >= _SCHEMA_VERSION_UTC_TIMESTAMPS:
            return
        rewritten = 0
        for table in _UTC_TIMESTAMP_TABLES:
            if not await self._table_exists(table):
                continue
            async with self._db.execute(
                f"SELECT rowid, timestamp FROM {table} WHERE timestamp NOT LIKE '%+00:00'"
            ) as cursor:
                rows = await cursor.fetchall()
            updates = []
            for rowid, raw in rows:
                try:
                    ts = _utc_ts(datetime.fromisoformat(raw))
                except (TypeError, ValueError):
                    _log.warning("Unparseable timestamp %r in %s left as is", raw, table)
                    continue
                if ts != raw:
                    updates.append((ts, rowid))
            await self._db.executemany(
                f"UPDATE OR REPLACE {table} SET timestamp = ? WHERE rowid = ?", updates
            )
            rewritten += len(updates)
        await self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_UTC_TIMESTAMPS}")
        await self._db.commit()
        if rewritten:
            _log.info("Converted %d history timestamps to UTC", rewritten)

    async def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        """``ALTER TABLE … ADD COLUMN`` for every column *table* still lacks."""
        assert self._db is not None
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    async def stop(self) -> None:
        """Store the open history buckets and close the database connection."""
        if self._db is not None:
            await self.flush()
            await self._db.close()
            self._db = None

    async def _table_exists(self, table: str) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ) as cursor:
            return await cursor.fetchone() is not None

    # ------------------------------------------------------------------
    # Device history: 5-minute and hourly tiers
    # ------------------------------------------------------------------

    async def write(self, measurement: Measurement) -> None:
        """Add a reading to its device's 5-minute bucket.

        The bucket is stored when the next one begins and about once a minute
        in between; ``flush()`` stores all open buckets now.  A reading with
        the timestamp of the previous one (a device polled faster than it
        reports) is counted once.
        """
        assert self._db is not None, "Call start() before write()"
        device_id = measurement.device_id
        ts = _as_utc(measurement.timestamp)
        if self._last_seen.get(device_id) == ts:
            return
        self._last_seen[device_id] = ts
        start = _bucket_start(ts)
        bucket = self._buckets.get(device_id)
        if bucket is None or bucket.start != start:
            if bucket is not None:
                await self._store_bucket(device_id, bucket)
            bucket = await self._load_bucket(device_id, start)
            self._buckets[device_id] = bucket
        bucket.add(measurement.power_w, measurement.soc_pct)
        if time.monotonic() - bucket.stored_at >= _FLUSH_INTERVAL_S:
            await self._store_bucket(device_id, bucket)

    async def _load_bucket(self, device_id: str, start: datetime) -> _Bucket:
        """The bucket as stored so far (after a restart, or for a late
        reading), else an empty one."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT pos_w, neg_w, soc_pct, samples FROM history_5m WHERE device_id = ? AND timestamp = ?",
            (device_id, _utc_ts(start)),
        ) as cursor:
            row = await cursor.fetchone()
        bucket = _Bucket(start=start, stored_at=time.monotonic())
        if row is not None:
            pos, neg, soc, n = row
            bucket.samples = int(n or 0)
            bucket.pos_sum = (pos or 0.0) * bucket.samples
            bucket.neg_sum = (neg or 0.0) * bucket.samples
            bucket.soc = soc
        return bucket

    async def _store_bucket(self, device_id: str, bucket: _Bucket) -> None:
        assert self._db is not None
        bucket.stored_at = time.monotonic()
        if not bucket.dirty:
            return
        bucket.dirty = False
        n = bucket.samples
        await self._db.execute(
            _UPSERT_HISTORY_5M,
            (
                device_id, _utc_ts(bucket.start),
                bucket.pos_sum / n if n else None,
                bucket.neg_sum / n if n else None,
                bucket.soc, n,
            ),
        )
        await self._db.commit()

    async def flush(self, device_id: str | None = None) -> None:
        """Store the open bucket of *device_id* (default: of every device)."""
        for did, bucket in list(self._buckets.items()):
            if device_id is None or did == device_id:
                await self._store_bucket(did, bucket)

    async def query_history(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
        *,
        hourly: bool = False,
    ) -> list[HistoryRow]:
        """The 5-minute (or hourly) rows of *device_id* that begin in ``[start, end]``.

        The open bucket is included.  The hourly tier ends with the last
        complete hour that ``maintain_history()`` has rolled up.
        """
        assert self._db is not None, "Call start() before query_history()"
        await self.flush(device_id)
        table = "history_1h" if hourly else "history_5m"
        async with self._db.execute(
            f"SELECT timestamp, pos_w, neg_w, soc_pct FROM {table} "
            "WHERE device_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (device_id, _utc_ts(start), _utc_ts(end)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [HistoryRow(datetime.fromisoformat(r[0]), r[1], r[2], r[3]) for r in rows]

    async def query_power_soc(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, float | None, float | None]]:
        """``(bucket start, mean power_w, last soc_pct)`` per 5 minutes for
        *device_id* in ``[start, end]``."""
        rows = await self.query_history(device_id, start, end)
        return [(r.timestamp, r.power_w, r.soc_pct) for r in rows]

    async def history_bounds(self) -> dict[str, str | None]:
        """Start of the oldest row of each tier (ISO, UTC), for the API."""
        assert self._db is not None
        out: dict[str, str | None] = {}
        for key, table in (("five_minutes_from", "history_5m"), ("hourly_from", "history_1h")):
            async with self._db.execute(f"SELECT min(timestamp) FROM {table}") as cursor:
                row = await cursor.fetchone()
            out[key] = row[0] if row else None
        return out

    async def maintain_history(self, now: datetime | None = None) -> int:
        """Roll finished hours up into the hourly tier and drop expired
        5-minute rows.  Returns the number of 5-minute rows deleted."""
        assert self._db is not None, "Call start() before maintain_history()"
        now = _as_utc(now or datetime.now(UTC))
        await self.flush()
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        # From the last hour already rolled up (again: late readings may have
        # completed it), or from the oldest 5-minute row.
        async with self._db.execute("SELECT max(timestamp) FROM history_1h") as cursor:
            last = (await cursor.fetchone())[0]
        if last is None:
            async with self._db.execute("SELECT min(timestamp) FROM history_5m") as cursor:
                last = (await cursor.fetchone())[0]
        if last is not None:
            first_hour = datetime.fromisoformat(last).replace(minute=0, second=0, microsecond=0)
            await self._db.execute(_ROLL_UP_HOURS, (_utc_ts(first_hour), _utc_ts(this_hour)))
            await self._db.commit()
        async with self._db.execute(
            "DELETE FROM history_5m WHERE timestamp < ?", (_utc_ts(this_hour - HISTORY_5M_RETENTION),)
        ) as cursor:
            deleted = max(cursor.rowcount, 0)
        await self._db.commit()
        return deleted

    async def migrate_raw_measurements(self, chunk: timedelta = timedelta(hours=6)) -> int:
        """Roll the ``measurements`` table of an older database up into the
        tiers and remove it.  Returns the number of raw rows processed.

        Works through the table oldest first in *chunk*-sized windows, each
        its own transaction: other writes interleave, and an interrupted run
        continues where it stopped.  Rows that are already in the tiers (the
        hours since the upgrade) are kept as they are.
        """
        assert self._db is not None, "Call start() before migrate_raw_measurements()"
        if not self._has_raw:
            return 0
        done = 0
        while True:
            async with self._db.execute("SELECT min(timestamp) FROM measurements") as cursor:
                first = (await cursor.fetchone())[0]
            if first is None:
                break
            try:
                start = datetime.fromisoformat(first).replace(minute=0, second=0, microsecond=0)
            except ValueError:
                _log.warning("Unparseable timestamp %r in measurements: row dropped", first)
                await self._db.execute("DELETE FROM measurements WHERE timestamp = ?", (first,))
                await self._db.commit()
                continue
            lo, hi = _utc_ts(start), _utc_ts(start + chunk)
            await self._db.execute(_ROLL_UP_RAW, (lo, hi))
            # ``first`` may sort below ``lo`` only by its sub-second part.
            async with self._db.execute("DELETE FROM measurements WHERE timestamp < ?", (hi,)) as cursor:
                done += max(cursor.rowcount, 0)
            await self._db.commit()
        await self._db.execute("DROP TABLE measurements")
        await self._db.commit()
        self._has_raw = False
        await self.maintain_history()
        async with self._db.execute("PRAGMA incremental_vacuum") as cursor:
            await cursor.fetchall()
        await self._db.commit()
        return done

    async def purge_before(
        self,
        cutoff: datetime,
        batch_size: int = _PURGE_BATCH_SIZE,
    ) -> int:
        """Delete ledger history, plan snapshots and the control execution
        log older than *cutoff* (and raw measurements, while an older
        database still has them).  The device history tiers have their own
        retention: see ``maintain_history()``.

        Deletes in committed batches of *batch_size* rows (see
        ``_PURGE_BATCH_SQL``).  Freed pages are returned to the filesystem via
        ``incremental_vacuum`` (a no-op until ``enable_incremental_vacuum()``
        has converted the DB).  Returns the number of rows deleted.
        """
        assert self._db is not None, "Call start() before purge_before()"
        ts = _utc_ts(cutoff)
        deleted = 0
        next_log = 1_000_000
        for table, sql in _PURGE_BATCH_SQL.items():
            if table == "measurements" and not self._has_raw:
                continue
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
        # Small tables (≈100 rows a day each): one statement is enough.
        for sql in _PURGE_DIAGNOSTICS_SQL:
            async with self._db.execute(sql, (_utc_iso(cutoff),)) as cursor:
                deleted += max(cursor.rowcount, 0)
            await self._db.commit()
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

    async def save_ledger_state(
        self,
        device_id: str,
        cost_basis: float,
        stored_energy_kwh: float,
    ) -> None:
        """Persist the current ledger state for *device_id* (upsert)."""
        assert self._db is not None, "Call start() before save_ledger_state()"
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
        ts = _utc_ts(timestamp or datetime.now(timezone.utc))
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
            (device_id, _utc_ts(start), _utc_ts(end)),
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
    # Vehicle sessions
    # ------------------------------------------------------------------

    async def open_vehicle_session(
        self, charger_id: str, vehicle_id: str | None, plugged_at: datetime,
        started_at: datetime, source: str,
    ) -> int:
        """Record a vehicle (or guest, ``None``) at a charger; returns the row id."""
        assert self._db is not None
        cur = await self._db.execute(
            "INSERT INTO vehicle_sessions (charger_id, vehicle_id, plugged_at, started_at, source)"
            " VALUES (?, ?, ?, ?, ?)",
            (charger_id, vehicle_id, plugged_at.isoformat(), started_at.isoformat(), source),
        )
        await self._db.commit()
        return int(cur.lastrowid or 0)

    async def close_vehicle_sessions(self, charger_id: str, ended_at: datetime) -> None:
        """End every open session at *charger_id*."""
        assert self._db is not None
        await self._db.execute(
            "UPDATE vehicle_sessions SET ended_at = ? WHERE charger_id = ? AND ended_at IS NULL",
            (ended_at.isoformat(), charger_id),
        )
        await self._db.commit()

    async def load_open_vehicle_sessions(self) -> list[dict]:
        """Open sessions: ``charger_id, vehicle_id, plugged_at, source, id`` (newest per charger)."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, charger_id, vehicle_id, plugged_at, source FROM vehicle_sessions"
            " WHERE ended_at IS NULL ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        latest: dict[str, dict] = {}
        for row_id, charger_id, vehicle_id, plugged_at, source in rows:
            latest[charger_id] = {
                "id": row_id, "charger_id": charger_id, "vehicle_id": vehicle_id,
                "plugged_at": datetime.fromisoformat(plugged_at), "source": source,
            }
        return list(latest.values())

    async def save_next_vehicles(self, choices: dict[str, str]) -> None:
        """Replace the pre-selected cars for the chargers' next plug-ins."""
        assert self._db is not None
        await self._db.execute("DELETE FROM charger_next_vehicle")
        await self._db.executemany(
            "INSERT INTO charger_next_vehicle (charger_id, choice) VALUES (?, ?)",
            list(choices.items()),
        )
        await self._db.commit()

    async def load_next_vehicles(self) -> dict[str, str]:
        assert self._db is not None
        async with self._db.execute("SELECT charger_id, choice FROM charger_next_vehicle") as cur:
            return {c: v for c, v in await cur.fetchall()}

    async def save_guest_modes(self, modes: dict[str, str]) -> None:
        """Replace the charging mode chosen per charger for its guest car."""
        assert self._db is not None
        await self._db.execute("DELETE FROM charger_guest_mode")
        await self._db.executemany(
            "INSERT INTO charger_guest_mode (charger_id, mode) VALUES (?, ?)", list(modes.items()),
        )
        await self._db.commit()

    async def load_guest_modes(self) -> dict[str, str]:
        assert self._db is not None
        async with self._db.execute("SELECT charger_id, mode FROM charger_guest_mode") as cur:
            return {c: m for c, m in await cur.fetchall()}

    async def query_vehicle_sessions(self, start: datetime, end: datetime) -> list[dict]:
        """Sessions overlapping ``[start, end)``, oldest first."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT charger_id, vehicle_id, plugged_at, started_at, ended_at, source"
            " FROM vehicle_sessions WHERE started_at < ? AND (ended_at IS NULL OR ended_at >= ?)"
            " ORDER BY started_at",
            (end.isoformat(), start.isoformat()),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"charger_id": c, "vehicle_id": v, "plugged_at": p, "started_at": s_, "ended_at": e,
             "source": src}
            for c, v, p, s_, e, src in rows
        ]

    # ------------------------------------------------------------------
    # Savings accounting
    # ------------------------------------------------------------------

    async def save_savings_slot(self, slot: SavingsSlot) -> None:
        """Persist the result of one accounting slot."""
        assert self._db is not None, "Call start() before save_savings_slot()"
        await self._db.execute(
            "INSERT OR REPLACE INTO savings_slots (slot_start, hours, real_cost,"
            " uncontrolled_cost, real_bill, uncontrolled_bill, ev_saving,"
            " real_battery_kwh, uncontrolled_battery_kwh, real_ev_kwh, uncontrolled_ev_kwh,"
            " real_stored_kwh, uncontrolled_stored_kwh, import_price, stored_detail,"
            " components, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utc_iso(slot.start), slot.hours, slot.real_cost, slot.uncontrolled_cost,
                slot.real_bill, slot.uncontrolled_bill, slot.ev_saving,
                slot.real_battery_kwh, slot.uncontrolled_battery_kwh,
                slot.real_ev_kwh, slot.uncontrolled_ev_kwh,
                sum(slot.real_stored.values()), sum(slot.uncontrolled_stored.values()),
                slot.import_price,
                json.dumps({"real": slot.real_stored, "uncontrolled": slot.uncontrolled_stored}),
                json.dumps(slot.components),
                json.dumps({"evs": slot.evs, "devices": slot.devices}),
            ),
        )
        await self._db.commit()

    async def apply_savings_correction(self, correction: SlotCorrection) -> bool:
        """Add a correction to a stored slot; False if the slot is not stored."""
        assert self._db is not None, "Call start() before apply_savings_correction()"
        key = _utc_iso(correction.start)
        async with self._db.execute(
            "SELECT components, detail FROM savings_slots WHERE slot_start = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        components = json.loads(row[0]) if row[0] else {}
        components[correction.component] = (
            components.get(correction.component, 0.0) + correction.ev_saving
        )
        detail = json.loads(row[1]) if row[1] else {}
        ev = detail.setdefault("evs", {}).setdefault(
            correction.component, {"real_kwh": 0.0, "unc_kwh": 0.0}
        )
        ev["unc_kwh"] = ev.get("unc_kwh", 0.0) + correction.uncontrolled_ev_kwh
        await self._db.execute(
            "UPDATE savings_slots SET"
            " uncontrolled_ev_kwh = COALESCE(uncontrolled_ev_kwh, 0) + ?,"
            " uncontrolled_cost = uncontrolled_cost + ?,"
            " uncontrolled_bill = uncontrolled_bill + ?,"
            " real_cost = real_cost + ?,"
            " ev_saving = ev_saving + ?,"
            " components = ?, detail = ?"
            " WHERE slot_start = ?",
            (
                correction.uncontrolled_ev_kwh, correction.uncontrolled_cost,
                correction.uncontrolled_cost, correction.real_cost, correction.ev_saving,
                json.dumps(components), json.dumps(detail), key,
            ),
        )
        await self._db.commit()
        return True

    async def query_savings_slots(self, start: datetime, end: datetime) -> list[dict]:
        """Savings slots in ``[start, end)``, oldest first."""
        assert self._db is not None, "Call start() before query_savings_slots()"
        async with self._db.execute(
            "SELECT slot_start, hours, real_cost, uncontrolled_cost, real_bill,"
            " uncontrolled_bill, ev_saving, real_battery_kwh, uncontrolled_battery_kwh,"
            " real_ev_kwh, uncontrolled_ev_kwh, real_stored_kwh, uncontrolled_stored_kwh,"
            " import_price, stored_detail, components, detail FROM savings_slots"
            " WHERE slot_start >= ? AND slot_start < ? ORDER BY slot_start",
            (_utc_iso(start), _utc_iso(end)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "slot_start": datetime.fromisoformat(r[0]),
                "hours": r[1],
                "real_cost": r[2],
                "uncontrolled_cost": r[3],
                "saving": r[3] - r[2],
                "real_bill": r[4],
                "uncontrolled_bill": r[5],
                "ev_saving": r[6],
                "real_battery_kwh": r[7],
                "uncontrolled_battery_kwh": r[8],
                "real_ev_kwh": r[9],
                "uncontrolled_ev_kwh": r[10],
                "real_stored_kwh": r[11],
                "uncontrolled_stored_kwh": r[12],
                "import_price": r[13],
                "stored_detail": json.loads(r[14]) if r[14] else None,
                "components": json.loads(r[15]) if r[15] else {},
                "detail": json.loads(r[16]) if r[16] else {},
            }
            for r in rows
        ]

    async def stored_ids(self) -> dict[str, set[str]]:
        """Ids that per-device tables hold rows for, by table (time series
        that are purged anyway are left out).  For the startup orphan check."""
        assert self._db is not None, "Call start() before stored_ids()"
        queries = {
            "ledger_state": "SELECT device_id FROM ledger_state",
            "ev_weekly_plan": "SELECT DISTINCT asset_id FROM ev_weekly_plan",
            "ev_day_overrides": "SELECT DISTINCT asset_id FROM ev_day_overrides",
            "ev_force_charge": "SELECT asset_id FROM ev_force_charge",
            "ev_disabled": "SELECT asset_id FROM ev_disabled",
            "vehicle_sessions": "SELECT DISTINCT charger_id FROM vehicle_sessions"
                                " WHERE ended_at IS NULL",
            "charger_next_vehicle": "SELECT charger_id FROM charger_next_vehicle",
            "charger_guest_mode": "SELECT charger_id FROM charger_guest_mode",
        }
        out: dict[str, set[str]] = {}
        for table, sql in queries.items():
            async with self._db.execute(sql) as cursor:
                out[table] = {str(r[0]) for r in await cursor.fetchall() if r[0] is not None}
        return out

    async def purge_savings(self, now: datetime) -> int:
        """Delete savings slots past their retention.  Returns the number of
        rows deleted."""
        assert self._db is not None, "Call start() before purge_savings()"
        deleted = 0
        for sql, days in _PURGE_SAVINGS_SQL:
            cutoff = _utc_iso(now - timedelta(days=days))
            async with self._db.execute(sql, (cutoff,)) as cursor:
                deleted += max(cursor.rowcount, 0)
        await self._db.commit()
        return deleted

    async def save_savings_state(self, state: dict) -> None:
        """Persist the savings tracker state (single row)."""
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


    # ------------------------------------------------------------------
    # Plan snapshots and control execution log
    # ------------------------------------------------------------------

    async def save_plan_snapshot(self, plan: EnergyPlan, day_ahead: bool = False) -> None:
        """Persist *plan*; ``day_ahead=True`` marks it as a day-ahead plan."""
        assert self._db is not None, "Call start() before save_plan_snapshot()"
        blob = zlib.compress(plan.model_dump_json().encode(), 6)
        await self._db.execute(
            "INSERT INTO plan_snapshots (created_at, day_ahead, plan) VALUES (?, ?, ?)"
            " ON CONFLICT(created_at) DO UPDATE SET"
            " day_ahead = MAX(day_ahead, excluded.day_ahead), plan = excluded.plan",
            (_utc_iso(plan.created_at), int(day_ahead), blob),
        )
        await self._db.commit()

    async def list_plan_snapshots(self, start: datetime, end: datetime) -> list[dict]:
        """``{created_at, day_ahead}`` of the plans made in ``[start, end)``."""
        assert self._db is not None, "Call start() before list_plan_snapshots()"
        async with self._db.execute(
            "SELECT created_at, day_ahead FROM plan_snapshots"
            " WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
            (_utc_iso(start), _utc_iso(end)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"created_at": r[0], "day_ahead": bool(r[1])} for r in rows]

    async def load_plan_snapshot(
        self, at: datetime, day_ahead_only: bool = False
    ) -> EnergyPlan | None:
        """The last plan made at or before *at* (optionally: day-ahead only)."""
        assert self._db is not None, "Call start() before load_plan_snapshot()"
        sql = "SELECT plan FROM plan_snapshots WHERE created_at <= ?"
        if day_ahead_only:
            sql += " AND day_ahead = 1"
        async with self._db.execute(
            sql + " ORDER BY created_at DESC LIMIT 1", (_utc_iso(at),)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return EnergyPlan.model_validate_json(zlib.decompress(row[0]))

    async def save_execution_slots(self, slots: "list[ExecutionSlot]") -> None:
        assert self._db is not None, "Call start() before save_execution_slots()"
        await self._db.executemany(
            "INSERT OR REPLACE INTO control_execution VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    _utc_iso(s.start), s.device_id, s.seconds, s.mode,
                    s.planned_w, s.setpoint_w, s.measured_w,
                    _utc_iso(s.plan_created_at) if s.plan_created_at else None,
                )
                for s in slots
            ],
        )
        await self._db.commit()

    async def query_execution(
        self, start: datetime, end: datetime, device_ids: "list[str] | None" = None
    ) -> list[dict]:
        """Execution rows with ``slot_start`` in ``[start, end)``, oldest first."""
        assert self._db is not None, "Call start() before query_execution()"
        sql = (
            "SELECT slot_start, device_id, seconds, mode, planned_w, setpoint_w,"
            " measured_w, plan_created_at FROM control_execution"
            " WHERE slot_start >= ? AND slot_start < ?"
        )
        params: list = [_utc_iso(start), _utc_iso(end)]
        if device_ids:
            sql += f" AND device_id IN ({', '.join('?' * len(device_ids))})"
            params += device_ids
        async with self._db.execute(sql + " ORDER BY slot_start, device_id", params) as cursor:
            rows = await cursor.fetchall()
        keys = (
            "slot_start", "device_id", "seconds", "mode", "planned_w",
            "setpoint_w", "measured_w", "plan_created_at",
        )
        return [dict(zip(keys, row)) for row in rows]


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


def _as_utc(ts: datetime) -> datetime:
    """*ts* in UTC; a naive datetime is taken to be UTC already."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _utc_ts(ts: datetime) -> str:
    """History timestamp text: ISO in UTC, sub-second precision kept.

    ``isoformat()`` omits a zero microsecond part, but "+" sorts before ".",
    so ``…:13+00:00`` still orders before ``…:13.5+00:00``.
    """
    return _as_utc(ts).isoformat()


def _utc_iso(ts: datetime) -> str:
    """ISO string in UTC — one fixed format, so text comparison orders by time."""
    return _as_utc(ts).isoformat(timespec="seconds")

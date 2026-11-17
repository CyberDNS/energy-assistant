"""Tests for sqlite storage startup behavior."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from energy_assistant.core.models import Measurement
from energy_assistant.storage.sqlite import SqliteStorageBackend


class _FakeConnection:
    async def execute(self, _query: str) -> None:
        return None

    async def commit(self) -> None:
        return None


class TestSqliteStorageBackendStart:
    async def test_creates_parent_dir_and_opens_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "data" / "energy-assistant.db"
        # Parent directory doesn't exist yet
        assert not db_path.parent.exists()

        backend = SqliteStorageBackend(db_path)
        await backend.start()
        await backend.stop()

        # Parent directory was created and DB file exists
        assert db_path.parent.exists()
        assert db_path.exists()

    async def test_raises_os_error_when_db_path_not_writable(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "energy-assistant.db"

        async def fake_open_db(path: Path):
            raise PermissionError("Permission denied")

        monkeypatch.setattr(SqliteStorageBackend, "_open_db", staticmethod(fake_open_db))

        backend = SqliteStorageBackend(db_path)
        with pytest.raises(PermissionError):
            await backend.start()


class TestSqliteStorageRetention:
    async def test_purge_before_deletes_only_old_rows(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        now = datetime.now(timezone.utc)
        old, recent = now - timedelta(days=10), now - timedelta(hours=1)
        for ts in (old, recent):
            await backend.write(Measurement(device_id="bat", timestamp=ts, power_w=1.0))
            await backend.append_ledger_history("bat", 0.2, 3.0, timestamp=ts)

        deleted = await backend.purge_before(now - timedelta(days=8))

        assert deleted == 2
        rows = await backend.query("bat", old - timedelta(days=1), now)
        assert [r.timestamp for r in rows] == [recent]
        hist = await backend.query_ledger_history("bat", old - timedelta(days=1), now)
        assert [h["t"] for h in hist] == [recent.isoformat()]
        await backend.stop()

    async def test_enable_incremental_vacuum_runs_once(self, tmp_path: Path) -> None:
        db_path = tmp_path / "h.db"
        backend = SqliteStorageBackend(db_path)
        await backend.start()
        assert await backend.enable_incremental_vacuum() is True
        assert await backend.enable_incremental_vacuum() is False
        await backend.stop()

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2

    async def test_purge_shrinks_file_after_incremental_vacuum(self, tmp_path: Path) -> None:
        db_path = tmp_path / "h.db"
        backend = SqliteStorageBackend(db_path)
        await backend.start()
        await backend.enable_incremental_vacuum()
        start = datetime.now(timezone.utc) - timedelta(days=30)
        for i in range(5000):
            await backend.write(
                Measurement(device_id="m", timestamp=start + timedelta(seconds=i), power_w=float(i))
            )
        size_full = db_path.stat().st_size

        await backend.purge_before(datetime.now(timezone.utc))
        await backend.stop()

        assert db_path.stat().st_size < size_full / 2

    async def test_drop_redundant_index(self, tmp_path: Path) -> None:
        db_path = tmp_path / "h.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE measurements (device_id TEXT NOT NULL, timestamp TEXT NOT NULL,"
                " power_w REAL, energy_kwh REAL, soc_pct REAL, extra TEXT,"
                " PRIMARY KEY (device_id, timestamp))"
            )
            conn.execute("CREATE INDEX idx_device_timestamp ON measurements (device_id, timestamp)")

        backend = SqliteStorageBackend(db_path)
        await backend.start()
        await backend.drop_redundant_index()
        await backend.stop()

        with sqlite3.connect(db_path) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_device_timestamp" not in names

    async def test_purge_in_batches_deletes_everything_expired(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=10)
        for i in range(10):
            await backend.write(
                Measurement(device_id="m", timestamp=start + timedelta(minutes=i), power_w=1.0)
            )
        await backend.write(Measurement(device_id="m", timestamp=now, power_w=1.0))

        deleted = await backend.purge_before(now - timedelta(days=1), batch_size=3)

        assert deleted == 10
        rows = await backend.query("m", start - timedelta(days=1), now)
        assert [r.timestamp for r in rows] == [now]
        await backend.stop()


_CET = timezone(timedelta(hours=1))


class TestSqliteStorageTimestampOffsets:
    """Timestamps with a non-UTC offset (e.g. Tibber's "+01:00") — issue #62."""

    async def test_offset_measurement_found_by_utc_window(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        ts = datetime(2026, 11, 17, 17, 49, 35, tzinfo=_CET)  # 16:49:35 UTC
        await backend.write(Measurement(device_id="tibber", timestamp=ts, power_w=500.0))

        utc = timezone.utc
        inside = await backend.query(
            "tibber", datetime(2026, 11, 17, 16, 0, tzinfo=utc), datetime(2026, 11, 17, 17, 0, tzinfo=utc)
        )
        # Lexically "17:49:35+01:00" lies inside 17:00–18:00 UTC, but the instant does not.
        outside = await backend.query(
            "tibber", datetime(2026, 11, 17, 17, 0, tzinfo=utc), datetime(2026, 11, 17, 18, 0, tzinfo=utc)
        )

        assert [m.timestamp for m in inside] == [ts]
        assert inside[0].timestamp.utcoffset() == timedelta(0)
        assert outside == []
        await backend.stop()

    async def test_query_bounds_with_offset(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        ts = datetime(2026, 11, 17, 16, 30, tzinfo=timezone.utc)
        await backend.write(Measurement(device_id="m", timestamp=ts, power_w=1.0))

        rows = await backend.query(
            "m", datetime(2026, 11, 17, 17, 0, tzinfo=_CET), datetime(2026, 11, 17, 18, 0, tzinfo=_CET)
        )

        assert [r.timestamp for r in rows] == [ts]
        await backend.stop()

    async def test_mixed_offsets_come_back_in_chronological_order(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        base = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
        instants = [base + timedelta(minutes=10 * i) for i in range(6)]
        # Alternate UTC and +01:00 inputs, written out of order.
        for i in (3, 0, 5, 1, 4, 2):
            ts = instants[i] if i % 2 else instants[i].astimezone(_CET)
            await backend.write(Measurement(device_id="m", timestamp=ts, power_w=float(i)))
            await backend.append_ledger_history("m", 0.2, float(i), timestamp=ts)

        rows = await backend.query("m", base, base + timedelta(hours=1))
        hist = await backend.query_ledger_history("m", base, base + timedelta(hours=1))

        assert [r.timestamp for r in rows] == instants
        assert [r.power_w for r in rows] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        assert [h["stored_energy_kwh"] for h in hist] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        assert all(h["t"].endswith("+00:00") for h in hist)
        await backend.stop()

    async def test_same_instant_with_different_offsets_is_one_row(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        ts = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
        await backend.write(Measurement(device_id="m", timestamp=ts, power_w=1.0))
        await backend.write(Measurement(device_id="m", timestamp=ts.astimezone(_CET), power_w=2.0))

        rows = await backend.query("m", ts - timedelta(minutes=1), ts + timedelta(minutes=1))

        assert [r.power_w for r in rows] == [2.0]
        await backend.stop()

    async def test_naive_timestamps_are_taken_as_utc(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        await backend.write(Measurement(device_id="m", timestamp=datetime(2026, 11, 17, 16, 0), power_w=1.0))

        rows = await backend.query(
            "m",
            datetime(2026, 11, 17, 15, 59, tzinfo=timezone.utc),
            datetime(2026, 11, 17, 16, 1, tzinfo=timezone.utc),
        )

        assert [r.timestamp for r in rows] == [datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)]
        await backend.stop()

    async def test_purge_respects_the_instant(self, tmp_path: Path) -> None:
        backend = SqliteStorageBackend(tmp_path / "h.db")
        await backend.start()
        cutoff = datetime(2026, 11, 17, 16, 30, tzinfo=timezone.utc)
        # 16:45 UTC: lexically "17:45+01:00" — kept either way.
        keep = datetime(2026, 11, 17, 17, 45, tzinfo=_CET)
        # 16:15 UTC: lexically "17:15+01:00" > "16:30+00:00", so a text
        # comparison would wrongly keep it.
        drop = datetime(2026, 11, 17, 17, 15, tzinfo=_CET)
        for ts in (drop, keep):
            await backend.write(Measurement(device_id="m", timestamp=ts, power_w=1.0))
            await backend.append_ledger_history("m", 0.2, 3.0, timestamp=ts)

        deleted = await backend.purge_before(cutoff)

        assert deleted == 2
        rows = await backend.query("m", cutoff - timedelta(hours=2), cutoff + timedelta(hours=2))
        assert [r.timestamp for r in rows] == [keep]
        await backend.stop()

    async def test_start_migrates_offset_rows_to_utc(self, tmp_path: Path) -> None:
        db_path = tmp_path / "h.db"
        backend = SqliteStorageBackend(db_path)
        await backend.start()
        await backend.stop()
        # Rows as older versions stored them: with the source's offset.
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA user_version = 0")
            conn.executemany(
                "INSERT INTO measurements (device_id, timestamp, power_w) VALUES (?, ?, ?)",
                [
                    ("tibber", "2026-11-17T17:49:35+01:00", 1.0),
                    ("tibber", "2026-11-17T16:49:00+00:00", 2.0),
                    # Same instant as the next row, once converted: a duplicate.
                    ("tibber", "2026-11-17T16:50:00+00:00", 3.0),
                    ("tibber", "2026-11-17T17:50:00+01:00", 4.0),
                    ("tibber", "2026-11-17T16:51:00", 5.0),  # naive: UTC
                    ("tibber", "garbage", 6.0),
                ],
            )
            conn.execute(
                "INSERT INTO ledger_history VALUES (?, ?, ?, ?)",
                ("bat", "2026-11-17T17:49:35.250000+01:00", 0.2, 3.0),
            )

        backend = SqliteStorageBackend(db_path)
        await backend.start()
        await backend.stop()

        with sqlite3.connect(db_path) as conn:
            meas = conn.execute(
                "SELECT timestamp, power_w FROM measurements ORDER BY timestamp"
            ).fetchall()
            hist = conn.execute("SELECT timestamp FROM ledger_history").fetchall()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert meas == [
            ("2026-11-17T16:49:00+00:00", 2.0),
            ("2026-11-17T16:49:35+00:00", 1.0),
            ("2026-11-17T16:50:00+00:00", 4.0),
            ("2026-11-17T16:51:00+00:00", 5.0),
            ("garbage", 6.0),
        ]
        assert hist == [("2026-11-17T16:49:35.250000+00:00",)]
        assert version >= 1

    async def test_migration_runs_only_once(self, tmp_path: Path) -> None:
        db_path = tmp_path / "h.db"
        backend = SqliteStorageBackend(db_path)
        await backend.start()
        await backend.stop()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO measurements (device_id, timestamp, power_w) VALUES (?, ?, ?)",
                ("m", "2026-11-17T17:49:35+01:00", 1.0),
            )

        backend = SqliteStorageBackend(db_path)
        await backend.start()
        await backend.stop()

        with sqlite3.connect(db_path) as conn:
            (ts,) = conn.execute("SELECT timestamp FROM measurements").fetchone()
        assert ts == "2026-11-17T17:49:35+01:00"  # already at the UTC schema version

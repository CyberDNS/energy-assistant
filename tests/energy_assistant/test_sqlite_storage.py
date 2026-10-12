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

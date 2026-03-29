"""Tests for sqlite storage startup behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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

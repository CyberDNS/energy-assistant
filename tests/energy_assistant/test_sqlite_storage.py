"""Tests for sqlite storage startup behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from energy_assistant.storage.sqlite import SqliteStorageBackend


class _FakeConnection:
    async def execute(self, _query: str) -> None:
        return None

    async def commit(self) -> None:
        return None


class TestSqliteStorageBackendStart:
    async def test_falls_back_to_ha_path_when_primary_fails(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        primary = tmp_path / "unwritable" / "state.db"
        fallback = tmp_path / "ha" / "energy-assistant.db"

        calls: list[Path] = []

        async def fake_open_db(path: Path):
            calls.append(path)
            if path == primary:
                raise sqlite3.OperationalError("unable to open database file")
            return _FakeConnection()

        monkeypatch.setattr(SqliteStorageBackend, "_open_db", staticmethod(fake_open_db))
        monkeypatch.setattr(
            SqliteStorageBackend,
            "_ha_fallback_db_path",
            staticmethod(lambda: fallback),
        )

        backend = SqliteStorageBackend(primary)
        await backend.start()

        assert calls == [primary, fallback]
        assert backend._db_path == fallback

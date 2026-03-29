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
    async def test_falls_back_to_first_available_ha_path_when_primary_fails(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        primary = tmp_path / "unwritable" / "state.db"
        fallback_data = tmp_path / "ha-data" / "energy-assistant.db"
        fallback_config = tmp_path / "ha-config" / "energy-assistant.db"

        calls: list[Path] = []

        async def fake_open_db(path: Path):
            calls.append(path)
            if path == primary:
                raise sqlite3.OperationalError("unable to open database file")
            return _FakeConnection()

        monkeypatch.setattr(SqliteStorageBackend, "_open_db", staticmethod(fake_open_db))
        monkeypatch.setattr(
            SqliteStorageBackend,
            "_ha_fallback_db_paths",
            staticmethod(lambda: [fallback_data, fallback_config]),
        )

        backend = SqliteStorageBackend(primary)
        await backend.start()

        assert calls == [primary, fallback_data]
        assert backend._db_path == fallback_data

    async def test_raises_clear_error_when_all_paths_fail(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        primary = tmp_path / "unwritable" / "state.db"
        fallback_data = tmp_path / "ha-data" / "energy-assistant.db"
        fallback_config = tmp_path / "ha-config" / "energy-assistant.db"

        async def fake_open_db(path: Path):
            if path == primary:
                raise sqlite3.OperationalError("unable to open database file")
            if path == fallback_data:
                raise sqlite3.OperationalError("unable to open database file")
            raise PermissionError("permission denied")

        monkeypatch.setattr(SqliteStorageBackend, "_open_db", staticmethod(fake_open_db))
        monkeypatch.setattr(
            SqliteStorageBackend,
            "_ha_fallback_db_paths",
            staticmethod(lambda: [fallback_data, fallback_config]),
        )

        backend = SqliteStorageBackend(primary)
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            await backend.start()

        message = str(exc_info.value)
        assert str(primary) in message
        assert str(fallback_data) in message
        assert str(fallback_config) in message

"""Tests for SqliteStorageBackend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from energy_manager.core.models import Measurement
from energy_manager.storage.sqlite import SqliteStorageBackend


def _ts(offset_seconds: int = 0) -> datetime:
    base = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


def _measurement(
    device_id: str = "solar",
    offset_seconds: int = 0,
    power_w: float | None = 1000.0,
) -> Measurement:
    return Measurement(
        device_id=device_id,
        timestamp=_ts(offset_seconds),
        power_w=power_w,
    )


@pytest.fixture
async def backend(tmp_db: Path) -> SqliteStorageBackend:
    b = SqliteStorageBackend(tmp_db)
    await b.start()
    yield b
    await b.stop()


async def test_write_and_query(backend: SqliteStorageBackend) -> None:
    await backend.write(_measurement(power_w=500.0))
    results = await backend.query("solar", _ts(-1), _ts(1))
    assert len(results) == 1
    assert results[0].power_w == 500.0
    assert results[0].device_id == "solar"


async def test_query_empty_returns_empty(backend: SqliteStorageBackend) -> None:
    results = await backend.query("solar", _ts(0), _ts(60))
    assert results == []


async def test_query_respects_time_bounds(backend: SqliteStorageBackend) -> None:
    for i in range(5):
        await backend.write(_measurement(offset_seconds=i * 60))

    # Query only the middle three (t=60, t=120, t=180)
    results = await backend.query("solar", _ts(60), _ts(180))
    assert len(results) == 3
    assert results[0].timestamp == _ts(60)
    assert results[-1].timestamp == _ts(180)


async def test_query_ordered_ascending(backend: SqliteStorageBackend) -> None:
    for i in [3, 1, 2, 0]:
        await backend.write(_measurement(offset_seconds=i * 10))

    results = await backend.query("solar", _ts(-1), _ts(100))
    timestamps = [r.timestamp for r in results]
    assert timestamps == sorted(timestamps)


async def test_query_filters_by_device_id(backend: SqliteStorageBackend) -> None:
    await backend.write(_measurement(device_id="solar", offset_seconds=0))
    await backend.write(_measurement(device_id="battery", offset_seconds=0))

    solar_results = await backend.query("solar", _ts(-1), _ts(1))
    assert all(r.device_id == "solar" for r in solar_results)
    assert len(solar_results) == 1


async def test_write_preserves_optional_fields(backend: SqliteStorageBackend) -> None:
    m = Measurement(
        device_id="battery",
        timestamp=_ts(),
        power_w=-500.0,
        energy_kwh=12.5,
        soc_pct=75.0,
        extra={"vendor": "pylontech"},
    )
    await backend.write(m)
    results = await backend.query("battery", _ts(-1), _ts(1))
    assert len(results) == 1
    r = results[0]
    assert r.energy_kwh == 12.5
    assert r.soc_pct == 75.0
    assert r.extra["vendor"] == "pylontech"


async def test_write_with_none_fields(backend: SqliteStorageBackend) -> None:
    m = Measurement(device_id="meter", timestamp=_ts())
    await backend.write(m)
    results = await backend.query("meter", _ts(-1), _ts(1))
    assert results[0].power_w is None
    assert results[0].soc_pct is None


async def test_upsert_replaces_on_same_primary_key(backend: SqliteStorageBackend) -> None:
    ts = _ts()
    await backend.write(Measurement(device_id="solar", timestamp=ts, power_w=1000.0))
    await backend.write(Measurement(device_id="solar", timestamp=ts, power_w=1200.0))

    results = await backend.query("solar", _ts(-1), _ts(1))
    assert len(results) == 1
    assert results[0].power_w == 1200.0


async def test_start_creates_schema_idempotently(tmp_db: Path) -> None:
    # Starting twice must not raise (CREATE TABLE IF NOT EXISTS).
    b = SqliteStorageBackend(tmp_db)
    await b.start()
    await b.stop()
    await b.start()
    await b.stop()

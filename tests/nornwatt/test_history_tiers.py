"""Device history: 5-minute and hourly tiers, and the roll-up of the raw
``measurements`` table of older databases."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nornwatt.core.battery_efficiency import find_balance_windows
from nornwatt.core.models import Measurement
from nornwatt.storage.sqlite import HISTORY_5M_RETENTION, SqliteStorageBackend

# The table of every reading that databases from before the tiers have.
_LEGACY_MEASUREMENTS = (
    "CREATE TABLE measurements (device_id TEXT NOT NULL, timestamp TEXT NOT NULL,"
    " power_w REAL, energy_kwh REAL, soc_pct REAL, extra TEXT,"
    " PRIMARY KEY (device_id, timestamp))"
)

T0 = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
FAR = (T0 - timedelta(days=400), T0 + timedelta(days=400))


@pytest.fixture
async def backend(tmp_path: Path):
    b = SqliteStorageBackend(tmp_path / "h.db")
    await b.start()
    yield b
    await b.stop()


async def _write(b: SqliteStorageBackend, ts: datetime, power: float | None, soc: float | None = None,
                 device: str = "bat") -> None:
    await b.write(Measurement(device_id=device, timestamp=ts, power_w=power, soc_pct=soc))


async def test_readings_become_one_row_per_five_minutes(backend) -> None:
    # 16:00–16:05: 100 W then −300 W; 16:05–16:10: 50 W.
    await _write(backend, T0 + timedelta(seconds=10), 100.0, 40.0)
    await _write(backend, T0 + timedelta(minutes=4), -300.0, 41.0)
    await _write(backend, T0 + timedelta(minutes=6), 50.0, 42.0)

    rows = await backend.query_history("bat", *FAR)

    assert [r.timestamp for r in rows] == [T0, T0 + timedelta(minutes=5)]
    first, second = rows
    # Import and export are kept apart, so they do not cancel out…
    assert (first.pos_w, first.neg_w) == (50.0, 150.0)
    # …and the mean power is their difference.
    assert first.power_w == -100.0
    assert first.soc_pct == 41.0  # the last reading of the bucket
    assert (second.power_w, second.soc_pct) == (50.0, 42.0)
    assert await backend.query_power_soc("bat", *FAR) == [(T0, -100.0, 41.0), (T0 + timedelta(minutes=5), 50.0, 42.0)]
    assert await backend.query_history("other", *FAR) == []


async def test_offsets_and_naive_timestamps_land_in_the_utc_bucket(backend) -> None:
    cet = timezone(timedelta(hours=1))
    await _write(backend, datetime(2026, 11, 17, 17, 1, tzinfo=cet), 10.0)   # 16:01 UTC
    await _write(backend, datetime(2026, 11, 17, 16, 2), 30.0)               # naive: UTC

    rows = await backend.query_history("bat", T0, datetime(2026, 11, 17, 17, 4, tzinfo=cet))

    assert [(r.timestamp, r.power_w) for r in rows] == [(T0, 20.0)]


async def test_a_reading_polled_twice_counts_once(backend) -> None:
    ts = T0 + timedelta(seconds=30)
    await _write(backend, ts, 100.0)
    await _write(backend, ts, 100.0)        # the device has not reported anything new
    await _write(backend, T0 + timedelta(seconds=90), 400.0)

    (row,) = await backend.query_history("bat", *FAR)
    assert row.power_w == 250.0


async def test_an_open_bucket_survives_a_restart(tmp_path: Path) -> None:
    b = SqliteStorageBackend(tmp_path / "h.db")
    await b.start()
    await _write(b, T0 + timedelta(seconds=10), 100.0, 40.0)
    await b.stop()

    b = SqliteStorageBackend(tmp_path / "h.db")
    await b.start()
    await _write(b, T0 + timedelta(minutes=2), 300.0)
    (row,) = await b.query_history("bat", *FAR)
    await b.stop()

    assert row.power_w == 200.0     # both readings, not only the one after the restart
    assert row.soc_pct == 40.0


async def test_readings_without_power_keep_the_state_of_charge(backend) -> None:
    await _write(backend, T0 + timedelta(seconds=5), None, 55.0, device="car")

    (row,) = await backend.query_history("car", *FAR)
    assert (row.power_w, row.soc_pct) == (None, 55.0)


async def test_hours_are_rolled_up_and_old_five_minute_rows_dropped(backend) -> None:
    now = datetime.now(UTC)
    old = (now - HISTORY_5M_RETENTION - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    recent = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    for hour in (old, recent):
        await _write(backend, hour + timedelta(minutes=1), 1000.0, 50.0)     # 00–05
        await _write(backend, hour + timedelta(minutes=31), -200.0, 48.0)    # 30–35
    await _write(backend, now, 5.0)                                          # the open hour

    deleted = await backend.maintain_history(now)

    assert deleted == 2
    hourly = await backend.query_history("bat", *FAR, hourly=True)
    assert [r.timestamp for r in hourly] == [old, recent]   # whole hours only
    for row in hourly:
        assert (row.pos_w, row.neg_w, row.power_w) == (500.0, 100.0, 400.0)
        assert row.soc_pct == 48.0
    fine = await backend.query_history("bat", *FAR)
    assert old not in {r.timestamp.replace(minute=0) for r in fine}
    # Again an hour later: nothing is lost or doubled.
    await backend.maintain_history(now)
    assert await backend.query_history("bat", *FAR, hourly=True) == hourly
    bounds = await backend.history_bounds()
    assert bounds["hourly_from"] == old.isoformat()


async def test_raw_measurements_of_an_older_database_are_rolled_up_and_removed(tmp_path: Path) -> None:
    db_path = tmp_path / "h.db"
    day = datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0) - timedelta(days=2)
    raw = []
    for minute in range(20):                       # one reading a minute for 20 minutes
        ts = day + timedelta(minutes=minute, seconds=7, milliseconds=250)
        raw.append(("bat", ts.isoformat(), 600.0 if minute < 10 else -600.0, None, 50.0 + minute, None))
    raw.append(("pv", (day + timedelta(hours=7)).isoformat(), -1500.0, None, None, None))
    with sqlite3.connect(db_path) as conn:
        conn.execute(_LEGACY_MEASUREMENTS)
        conn.executemany("INSERT INTO measurements VALUES (?, ?, ?, ?, ?, ?)", raw)

    b = SqliteStorageBackend(db_path)
    await b.start()
    # A reading stored since the upgrade is kept as it is.
    await _write(b, day + timedelta(minutes=17), 0.0, 99.0)
    migrated = await b.migrate_raw_measurements(chunk=timedelta(hours=1))
    rows = await b.query_history("bat", *FAR)
    pv = await b.query_history("pv", *FAR)
    hourly = await b.query_history("bat", *FAR, hourly=True)
    again = await b.migrate_raw_measurements()
    await b.stop()

    assert migrated == len(raw) and again == 0
    assert [(r.timestamp - day).seconds // 60 for r in rows] == [0, 5, 10, 15]
    assert [r.power_w for r in rows[:3]] == [600.0, 600.0, -600.0]
    assert [r.soc_pct for r in rows[:3]] == [54.0, 59.0, 64.0]
    assert (rows[3].power_w, rows[3].soc_pct) == (0.0, 99.0)
    assert [(r.timestamp, r.neg_w) for r in pv] == [(day + timedelta(hours=7), 1500.0)]
    assert [r.timestamp for r in hourly] == [day]
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "measurements" not in tables


async def test_a_new_database_has_no_raw_table(tmp_path: Path) -> None:
    b = SqliteStorageBackend(tmp_path / "h.db")
    await b.start()
    assert await b.migrate_raw_measurements() == 0
    await b.purge_before(datetime.now(UTC))
    await b.stop()
    with sqlite3.connect(tmp_path / "h.db") as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"history_5m", "history_1h"} <= tables and "measurements" not in tables


async def test_battery_windows_are_found_in_five_minute_rows(backend) -> None:
    """The efficiency learning reads the 5-minute tier: a rest-to-rest window
    with known energies must still be found, with the same energies."""
    capacity = 10.0
    t = T0
    soc = 50.0

    async def run(minutes: int, power: float, d_soc_per_min: float) -> None:
        nonlocal t, soc
        for _ in range(minutes * 2):                  # a reading every 30 s
            await _write(backend, t, power, round(soc, 2))
            soc += d_soc_per_min / 2
            t += timedelta(seconds=30)

    await run(30, 0.0, 0.0)                           # rest at 50 %
    await run(60, 3000.0, 0.45)                       # 3 kWh in  → +27 %
    await run(60, -2430.0, -0.45)                     # 2.43 kWh out → back to 50 %
    await run(11 * 60, 0.0, 0.0)                      # rest, 12 h after the first rest

    samples = await backend.query_power_soc("bat", *FAR)
    windows = find_balance_windows(samples, capacity)

    assert len(windows) == 1
    assert windows[0].energy_in_kwh == pytest.approx(3.0, rel=0.02)
    assert windows[0].energy_out_kwh == pytest.approx(2.43, rel=0.02)
    assert windows[0].delta_stored_kwh == pytest.approx(0.0, abs=0.1)

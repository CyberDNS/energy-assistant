"""Tests for core/history_backfill.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from energy_assistant.core.config import AppConfig
from energy_assistant.core.history_backfill import (
    DifferentialSource,
    HaEntitySource,
    _combine_differential,
    _merge_presence,
    fetch_series,
    resolve_history_source,
    run_history_backfill,
)
from energy_assistant.core.learned_model_store import LearnedModelConfig, LearnedModelStore
from energy_assistant.storage.sqlite import SqliteStorageBackend
from helpers.fake_ha_client import FakeHAClient

_T0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def _cfg(devices: dict) -> AppConfig:
    return AppConfig(devices=devices)


# ---------------------------------------------------------------------------
# resolve_history_source
# ---------------------------------------------------------------------------


def test_resolve_leaf_ha_entity():
    cfg = _cfg({"climate_all": {"type": "generic_homeassistant", "oid_power": "sensor.x"}})
    source = resolve_history_source("climate_all", cfg)
    assert source == HaEntitySource(entity_id="sensor.x", invert_sign=False)


def test_resolve_leaf_ha_entity_with_invert_sign():
    cfg = _cfg({"pv": {"type": "generic_homeassistant", "oid_power": "sensor.x", "invert_sign": True}})
    source = resolve_history_source("pv", cfg)
    assert source == HaEntitySource(entity_id="sensor.x", invert_sign=True)


def test_resolve_unknown_device_returns_none():
    assert resolve_history_source("missing", _cfg({})) is None


def test_resolve_iobroker_backed_device_returns_none():
    cfg = _cfg({"household_meter": {"type": "tibber_iobroker"}})
    assert resolve_history_source("household_meter", cfg) is None


def test_resolve_differential_both_legs_ha_backed():
    cfg = _cfg({
        "grid": {"type": "generic_homeassistant", "oid_power": "sensor.grid"},
        "house": {"type": "generic_homeassistant", "oid_power": "sensor.house"},
        "heatpump": {
            "type": "differential", "minuend": "grid", "subtrahend": "house", "min_w": 0.0,
        },
    })
    source = resolve_history_source("heatpump", cfg)
    assert source == DifferentialSource(
        minuend=HaEntitySource(entity_id="sensor.grid"),
        subtrahend=HaEntitySource(entity_id="sensor.house"),
        min_power_w=0.0,
        max_power_w=None,
    )


def test_resolve_differential_with_iobroker_leg_returns_none():
    """Mirrors the real config: heatpump_meter's subtrahend (household_meter) is
    ioBroker/Tibber-backed, so the differential as a whole can't be backfilled."""
    cfg = _cfg({
        "grid": {"type": "generic_homeassistant", "oid_power": "sensor.grid"},
        "house": {"type": "tibber_iobroker"},
        "heatpump": {"type": "differential", "minuend": "grid", "subtrahend": "house"},
    })
    assert resolve_history_source("heatpump", cfg) is None


def test_resolve_differential_with_nondefault_field_returns_none():
    cfg = _cfg({
        "grid": {"type": "generic_homeassistant", "oid_power": "sensor.grid"},
        "house": {"type": "generic_homeassistant", "oid_power": "sensor.house"},
        "heatpump": {
            "type": "differential", "minuend": "grid", "subtrahend": "house",
            "minuend_field": "extra.import_w",
        },
    })
    assert resolve_history_source("heatpump", cfg) is None


# ---------------------------------------------------------------------------
# _combine_differential / fetch_series
# ---------------------------------------------------------------------------


def test_combine_differential_subtracts_nearest_match():
    minuend = [(_T0, 1000.0), (_T0 + timedelta(minutes=10), 1200.0)]
    subtrahend = [(_T0, 300.0), (_T0 + timedelta(minutes=10), 400.0)]
    result = _combine_differential(minuend, subtrahend, min_power_w=0.0, max_power_w=None)
    assert result == [(_T0, 700.0), (_T0 + timedelta(minutes=10), 800.0)]


def test_combine_differential_clamps_to_min_power():
    minuend = [(_T0, 100.0)]
    subtrahend = [(_T0, 500.0)]  # would be -400 without clamping
    result = _combine_differential(minuend, subtrahend, min_power_w=0.0, max_power_w=None)
    assert result == [(_T0, 0.0)]


def test_combine_differential_empty_inputs():
    assert _combine_differential([], [(_T0, 1.0)], None, None) == []
    assert _combine_differential([(_T0, 1.0)], [], None, None) == []


@pytest.mark.asyncio
async def test_fetch_series_ha_entity_inverts_sign():
    client = FakeHAClient(history={"sensor.pv": [(_T0, "500.0")]})
    source = HaEntitySource(entity_id="sensor.pv", invert_sign=True)
    points = await fetch_series(source, client, _T0, _T0 + timedelta(hours=1))
    assert points == [(_T0, -500.0)]


@pytest.mark.asyncio
async def test_fetch_series_differential_combines_both_legs():
    client = FakeHAClient(history={
        "sensor.grid": [(_T0, "1000.0")],
        "sensor.house": [(_T0, "300.0")],
    })
    source = DifferentialSource(
        minuend=HaEntitySource(entity_id="sensor.grid"),
        subtrahend=HaEntitySource(entity_id="sensor.house"),
        min_power_w=0.0,
    )
    points = await fetch_series(source, client, _T0, _T0 + timedelta(hours=1))
    assert points == [(_T0, 700.0)]


@pytest.mark.asyncio
async def test_fetch_series_skips_non_numeric_states():
    client = FakeHAClient(history={"sensor.x": [(_T0, "unavailable"), (_T0 + timedelta(minutes=1), "42.0")]})
    source = HaEntitySource(entity_id="sensor.x")
    points = await fetch_series(source, client, _T0, _T0 + timedelta(hours=1))
    assert points == [(_T0 + timedelta(minutes=1), 42.0)]


# ---------------------------------------------------------------------------
# _merge_presence
# ---------------------------------------------------------------------------


def test_merge_presence_or_combines_two_people():
    person_a = [(_T0, "not_home"), (_T0 + timedelta(hours=1), "home")]
    person_b = [(_T0, "home"), (_T0 + timedelta(hours=2), "not_home")]
    merged = _merge_presence([person_a, person_b])
    values_by_ts = {ts: v for ts, v in merged}
    assert values_by_ts[_T0] == 1.0  # person_b home
    assert values_by_ts[_T0 + timedelta(hours=1)] == 1.0  # both home now
    assert values_by_ts[_T0 + timedelta(hours=2)] == 1.0  # person_a still home


def test_merge_presence_everyone_away():
    merged = _merge_presence([[(_T0, "not_home")], [(_T0, "not_home")]])
    assert merged == [(_T0, 0.0), (_T0, 0.0)]


# ---------------------------------------------------------------------------
# run_history_backfill (integration, in-memory sqlite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_history_backfill_no_ha_client_is_noop(tmp_path: Path):
    storage = SqliteStorageBackend(tmp_path / "db.sqlite")
    await storage.start()
    store = LearnedModelStore()
    await run_history_backfill(_cfg({}), None, storage, store)
    await storage.stop()  # no exception == success


@pytest.mark.asyncio
async def test_run_history_backfill_seeds_signals_and_devices(tmp_path: Path):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    cfg = AppConfig(
        devices={"climate_all": {"type": "generic_homeassistant", "oid_power": "sensor.climate"}},
        environment={
            "outdoor_temperature": "sensor.temp",
            "presence": ["person.a"],
        },
    )
    client = FakeHAClient(history={
        "sensor.temp": [(start, "20.0"), (start + timedelta(hours=1), "21.0")],
        "person.a": [(start, "home")],
        "sensor.climate": [(start, "300.0")],
    })
    storage = SqliteStorageBackend(tmp_path / "db.sqlite")
    await storage.start()
    store = LearnedModelStore()
    store.register("climate_all", LearnedModelConfig())

    await run_history_backfill(cfg, client, storage, store, backfill_days=2)

    temps = await storage.query_signals("outdoor_temperature", start - timedelta(days=1), now)
    presence = await storage.query_signals("anyone_home", start - timedelta(days=1), now)
    measurements = await storage.query("climate_all", start - timedelta(days=1), now)
    assert len(temps) == 2
    assert len(presence) == 1
    assert len(measurements) == 1
    assert measurements[0].power_w == 300.0
    await storage.stop()


@pytest.mark.asyncio
async def test_run_history_backfill_is_idempotent(tmp_path: Path):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    cfg = AppConfig(
        devices={"climate_all": {"type": "generic_homeassistant", "oid_power": "sensor.climate"}},
        environment={"outdoor_temperature": "sensor.temp"},
    )
    client = FakeHAClient(history={
        "sensor.temp": [(start, "20.0")],
        "sensor.climate": [(start, "300.0")],
    })
    storage = SqliteStorageBackend(tmp_path / "db.sqlite")
    await storage.start()
    store = LearnedModelStore()
    store.register("climate_all", LearnedModelConfig())

    await run_history_backfill(cfg, client, storage, store, backfill_days=2)
    # Second call should not duplicate/error even though local data now exists.
    await run_history_backfill(cfg, client, storage, store, backfill_days=2)

    temps = await storage.query_signals("outdoor_temperature", start - timedelta(days=1), now)
    measurements = await storage.query("climate_all", start - timedelta(days=1), now)
    assert len(temps) == 1
    assert len(measurements) == 1
    await storage.stop()


@pytest.mark.asyncio
async def test_run_history_backfill_not_blocked_by_sparse_recent_only_data(tmp_path: Path):
    """Regression test: a device/signal with only recent live-polled rows
    (e.g. a dev DB reused across restarts, or live polling that started
    before any backfill ever ran) must NOT be mistaken for an already-
    completed backfill — otherwise real history is never pulled."""
    now = datetime.now(timezone.utc)
    # 1h inside the exact 10-day boundary: run_history_backfill() computes
    # its own `now` a moment later than this test's, so its `start`
    # (now_func - 10 days) is a hair *after* `now - 10 days` — a point
    # placed exactly at `now - 10 days` could fall just outside its fetch
    # window, so nudge it 1h more recent for safety margin.
    horizon_start = now - timedelta(days=9, hours=23)
    recent_only = now - timedelta(hours=1)  # far newer than horizon_start
    cfg = AppConfig(
        devices={"climate_all": {"type": "generic_homeassistant", "oid_power": "sensor.climate"}},
        environment={"outdoor_temperature": "sensor.temp"},
    )
    client = FakeHAClient(history={
        "sensor.temp": [(horizon_start, "20.0"), (recent_only, "21.0")],
        "sensor.climate": [(horizon_start, "300.0"), (recent_only, "310.0")],
    })
    storage = SqliteStorageBackend(tmp_path / "db.sqlite")
    await storage.start()
    # Pre-seed only a sparse, recent row — simulating live polling with no
    # prior backfill, as would exist on a reused/persistent dev DB.
    await storage.write_signal("outdoor_temperature", recent_only, 21.0)
    from energy_assistant.core.models import Measurement
    await storage.write(Measurement(device_id="climate_all", timestamp=recent_only, power_w=310.0))

    store = LearnedModelStore()
    store.register("climate_all", LearnedModelConfig())

    await run_history_backfill(cfg, client, storage, store, backfill_days=10)

    temps = await storage.query_signals("outdoor_temperature", horizon_start - timedelta(hours=1), now)
    measurements = await storage.query("climate_all", horizon_start - timedelta(hours=1), now)
    # The backfill should have run and pulled the older point too, not been
    # skipped just because a recent row already existed.
    assert any(p.timestamp == horizon_start for p in temps)
    assert any(m.timestamp == horizon_start for m in measurements)
    await storage.stop()


class _TimingOutHAClient(FakeHAClient):
    """Raises on get_history for specific entities — simulates a slow/failed
    HA history query (e.g. a ReadTimeout) to verify the backfill degrades
    gracefully instead of taking the whole server startup down with it."""

    def __init__(self, *, fails_for: set[str], **kwargs):
        super().__init__(**kwargs)
        self._fails_for = fails_for

    async def get_history(self, entity_id, start, end):
        if entity_id in self._fails_for:
            raise TimeoutError("simulated ReadTimeout")
        return await super().get_history(entity_id, start, end)


@pytest.mark.asyncio
async def test_run_history_backfill_survives_one_entity_timing_out(tmp_path: Path):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    cfg = AppConfig(
        devices={"climate_all": {"type": "generic_homeassistant", "oid_power": "sensor.climate"}},
        environment={"outdoor_temperature": "sensor.temp", "presence": ["person.a"]},
    )
    client = _TimingOutHAClient(
        fails_for={"sensor.temp"},
        history={
            "person.a": [(start, "home")],
            "sensor.climate": [(start, "300.0")],
        },
    )
    storage = SqliteStorageBackend(tmp_path / "db.sqlite")
    await storage.start()
    store = LearnedModelStore()
    store.register("climate_all", LearnedModelConfig())

    # Must not raise, despite outdoor_temperature timing out.
    await run_history_backfill(cfg, client, storage, store, backfill_days=2)

    temps = await storage.query_signals("outdoor_temperature", start - timedelta(days=1), now)
    presence = await storage.query_signals("anyone_home", start - timedelta(days=1), now)
    measurements = await storage.query("climate_all", start - timedelta(days=1), now)
    assert temps == []  # the failed one is simply absent, not crashed
    assert len(presence) == 1  # unrelated signals still get backfilled
    assert len(measurements) == 1
    await storage.stop()


@pytest.mark.asyncio
async def test_run_history_backfill_skips_device_without_history_source(tmp_path: Path):
    now = datetime.now(timezone.utc)
    cfg = AppConfig(devices={"household_meter": {"type": "tibber_iobroker"}})
    client = FakeHAClient()
    storage = SqliteStorageBackend(tmp_path / "db.sqlite")
    await storage.start()
    store = LearnedModelStore()
    store.register("household_meter", LearnedModelConfig())

    await run_history_backfill(cfg, client, storage, store, backfill_days=2)

    measurements = await storage.query("household_meter", now - timedelta(days=2), now)
    assert measurements == []
    await storage.stop()

"""Tests for TibberIoBrokerTariff."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers.fake_iobroker_client import FakeIoBrokerClient

from energy_manager.core.tariff import TariffModel
from energy_manager.plugins.tibber_iobroker.tariff import TibberIoBrokerTariff

# Fixed reference point used throughout so we never hit hour-boundary races.
_FIXED_NOW = datetime(2026, 3, 15, 8, 0, 0, tzinfo=timezone.utc)

# Fake home ID used for all tests.
_HOME = "test-home"
_TODAY_OID = f"tibberlink.0.Homes.{_HOME}.PricesToday.json"
_TOMORROW_OID = f"tibberlink.0.Homes.{_HOME}.PricesTomorrow.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tariff(
    today_json: str | None = None,
    tomorrow_json: str | None = None,
    include_tomorrow: bool = True,
    now: datetime = _FIXED_NOW,
) -> TibberIoBrokerTariff:
    values: dict = {}
    if today_json is not None:
        values[_TODAY_OID] = today_json
    if tomorrow_json is not None:
        values[_TOMORROW_OID] = tomorrow_json
    client = FakeIoBrokerClient(values)
    return TibberIoBrokerTariff(
        tariff_id="hauptstrom",
        client=client,
        home_id=_HOME,
        include_tomorrow=include_tomorrow,
        _now_func=lambda: now,
    )


def _schedule_json(
    count: int = 6,
    base_price: float = 0.28,
    base_dt: datetime = _FIXED_NOW,
    interval: timedelta = timedelta(hours=1),
) -> str:
    """Return a JSON schedule string in tibberlink format."""
    entries = [
        {
            "startsAt": (base_dt + interval * i).isoformat(),
            "total": round(base_price + i * 0.01, 4),
            "energy": round(base_price + i * 0.01 - 0.23, 4),
            "tax": 0.23,
            "currency": "EUR",
            "level": "NORMAL",
        }
        for i in range(count)
    ]
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_tariff_protocol() -> None:
    tariff = _tariff(today_json=_schedule_json())
    assert isinstance(tariff, TariffModel)


def test_tariff_id() -> None:
    assert _tariff().tariff_id == "hauptstrom"


# ---------------------------------------------------------------------------
# price_at
# ---------------------------------------------------------------------------


async def test_price_at_returns_active_slot() -> None:
    """price_at returns the most recently started slot price."""
    tariff = _tariff(today_json=_schedule_json(count=4, base_price=0.30))
    price = await tariff.price_at(_FIXED_NOW)
    assert price == pytest.approx(0.30)


async def test_price_at_picks_most_recent_slot() -> None:
    """price_at uses slot at t=7min (not t=15min) when queried at t=7min."""
    entries = [
        {"startsAt": _FIXED_NOW.isoformat(), "total": 0.28},
        {"startsAt": (_FIXED_NOW + timedelta(minutes=15)).isoformat(), "total": 0.35},
    ]
    tariff = _tariff(today_json=json.dumps(entries))
    dt = _FIXED_NOW + timedelta(minutes=7)
    assert await tariff.price_at(dt) == pytest.approx(0.28)


async def test_price_at_returns_zero_when_no_data() -> None:
    tariff = _tariff()  # no today_json
    assert await tariff.price_at(_FIXED_NOW) == 0.0


async def test_price_at_uses_15min_granularity() -> None:
    """price_at supports 15-minute slot resolution as used by tibberlink."""
    entries = [
        {"startsAt": _FIXED_NOW.isoformat(), "total": 0.28},
        {"startsAt": (_FIXED_NOW + timedelta(minutes=15)).isoformat(), "total": 0.35},
        {"startsAt": (_FIXED_NOW + timedelta(minutes=30)).isoformat(), "total": 0.32},
    ]
    tariff = _tariff(today_json=json.dumps(entries))
    # Query at 20 minutes in → second slot (0.35) is active
    assert await tariff.price_at(_FIXED_NOW + timedelta(minutes=20)) == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# price_schedule — from today only
# ---------------------------------------------------------------------------


async def test_price_schedule_returns_correct_count() -> None:
    tariff = _tariff(today_json=_schedule_json(count=6))
    schedule = await tariff.price_schedule(timedelta(hours=6))
    assert len(schedule) == 6


async def test_price_schedule_sorted_ascending() -> None:
    tariff = _tariff(today_json=_schedule_json(count=4))
    schedule = await tariff.price_schedule(timedelta(hours=4))
    ts = [pt.timestamp for pt in schedule]
    assert ts == sorted(ts)


async def test_price_schedule_prices_correct() -> None:
    tariff = _tariff(today_json=_schedule_json(count=3, base_price=0.28))
    schedule = await tariff.price_schedule(timedelta(hours=3))
    assert schedule[0].price_eur_per_kwh == pytest.approx(0.28)
    assert schedule[1].price_eur_per_kwh == pytest.approx(0.29)
    assert schedule[2].price_eur_per_kwh == pytest.approx(0.30)


async def test_price_schedule_15min_granularity() -> None:
    """15-minute slots are preserved from tibberlink data."""
    tariff = _tariff(
        today_json=_schedule_json(count=8, interval=timedelta(minutes=15)),
    )
    schedule = await tariff.price_schedule(timedelta(hours=2))
    assert len(schedule) == 8
    for i in range(1, len(schedule)):
        delta = schedule[i].timestamp - schedule[i - 1].timestamp
        assert delta == timedelta(minutes=15)


async def test_price_schedule_excludes_past_slots() -> None:
    """Slots before now are not included in the schedule."""
    past = _FIXED_NOW - timedelta(hours=1)
    entries = [
        {"startsAt": past.isoformat(), "total": 0.20},          # past → excluded
        {"startsAt": _FIXED_NOW.isoformat(), "total": 0.28},    # now  → included
        {"startsAt": (_FIXED_NOW + timedelta(hours=1)).isoformat(), "total": 0.30},
    ]
    tariff = _tariff(today_json=json.dumps(entries))
    schedule = await tariff.price_schedule(timedelta(hours=2))
    prices = [pt.price_eur_per_kwh for pt in schedule]
    assert 0.20 not in prices
    assert 0.28 in prices
    assert 0.30 in prices


async def test_price_schedule_falls_back_to_zeros_when_no_data() -> None:
    tariff = _tariff()  # no today/tomorrow json
    schedule = await tariff.price_schedule(timedelta(hours=3))
    assert len(schedule) == 3
    assert all(pt.price_eur_per_kwh == 0.0 for pt in schedule)


# ---------------------------------------------------------------------------
# price_schedule — today + tomorrow merging
# ---------------------------------------------------------------------------


async def test_price_schedule_merges_today_and_tomorrow() -> None:
    """Today + tomorrow entries are merged into a single sorted schedule."""
    tomorrow_start = _FIXED_NOW + timedelta(hours=3)
    tariff = _tariff(
        today_json=_schedule_json(count=3, base_price=0.28, base_dt=_FIXED_NOW),
        tomorrow_json=_schedule_json(count=3, base_price=0.32, base_dt=tomorrow_start),
    )
    schedule = await tariff.price_schedule(timedelta(hours=6))
    assert len(schedule) == 6
    assert schedule[0].price_eur_per_kwh == pytest.approx(0.28)
    assert schedule[3].price_eur_per_kwh == pytest.approx(0.32)
    ts = [pt.timestamp for pt in schedule]
    assert ts == sorted(ts)


async def test_price_schedule_without_tomorrow() -> None:
    """include_tomorrow=False means only today schedule is used."""
    tomorrow_start = _FIXED_NOW + timedelta(hours=2)
    tariff = _tariff(
        today_json=_schedule_json(count=2, base_price=0.28),
        tomorrow_json=_schedule_json(count=2, base_price=0.99, base_dt=tomorrow_start),
        include_tomorrow=False,
    )
    schedule = await tariff.price_schedule(timedelta(hours=4))
    prices = [pt.price_eur_per_kwh for pt in schedule]
    assert all(p < 0.5 for p in prices), "tomorrow prices leaked into schedule"


async def test_price_schedule_today_missing_falls_back_to_tomorrow() -> None:
    """If today's OID is absent, tomorrow's data is still used."""
    tomorrow_start = _FIXED_NOW + timedelta(hours=1)
    tariff = _tariff(
        today_json=None,  # missing
        tomorrow_json=_schedule_json(count=3, base_price=0.30, base_dt=tomorrow_start),
    )
    schedule = await tariff.price_schedule(timedelta(hours=4))
    assert len(schedule) == 3
    assert schedule[0].price_eur_per_kwh == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Malformed data handling
# ---------------------------------------------------------------------------


async def test_malformed_entries_skipped() -> None:
    entries = [
        {"startsAt": _FIXED_NOW.isoformat(), "total": 0.28},
        {"bad_key": "data"},  # missing startsAt and total → skipped
        {"startsAt": (_FIXED_NOW + timedelta(hours=1)).isoformat(), "total": 0.30},
    ]
    tariff = _tariff(today_json=json.dumps(entries))
    schedule = await tariff.price_schedule(timedelta(hours=3))
    prices = [pt.price_eur_per_kwh for pt in schedule]
    assert 0.28 in prices
    assert 0.30 in prices


async def test_malformed_json_falls_back_to_zeros() -> None:
    values = {_TODAY_OID: "not valid json"}
    client = FakeIoBrokerClient(values)
    tariff = TibberIoBrokerTariff(
        tariff_id="test",
        client=client,
        home_id=_HOME,
        _now_func=lambda: _FIXED_NOW,
    )
    schedule = await tariff.price_schedule(timedelta(hours=2))
    assert all(pt.price_eur_per_kwh == 0.0 for pt in schedule)

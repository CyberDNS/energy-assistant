"""Tests for FlatRateTariff."""

from __future__ import annotations

from datetime import timedelta, timezone, datetime

import pytest

from energy_manager.plugins.flat_rate.tariff import FlatRateTariff
from energy_manager.core.tariff import TariffModel


def test_satisfies_protocol() -> None:
    tariff = FlatRateTariff("default", 0.28)
    assert isinstance(tariff, TariffModel)


def test_tariff_id() -> None:
    assert FlatRateTariff("hauptstrom", 0.30).tariff_id == "hauptstrom"
    assert FlatRateTariff("waermepumpe", 0.19).tariff_id == "waermepumpe"


async def test_price_at_returns_fixed_price() -> None:
    tariff = FlatRateTariff("default", 0.28)
    dt = datetime.now(timezone.utc)
    assert await tariff.price_at(dt) == 0.28


async def test_price_at_is_time_independent() -> None:
    tariff = FlatRateTariff("default", 0.25)
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=30)
    assert await tariff.price_at(now) == await tariff.price_at(future)


async def test_price_schedule_length_matches_horizon() -> None:
    tariff = FlatRateTariff("default", 0.28)
    schedule = await tariff.price_schedule(timedelta(hours=24))
    assert len(schedule) == 24


async def test_price_schedule_short_horizon() -> None:
    tariff = FlatRateTariff("default", 0.28)
    schedule = await tariff.price_schedule(timedelta(hours=1))
    assert len(schedule) == 1


async def test_price_schedule_all_same_price() -> None:
    tariff = FlatRateTariff("default", 0.22)
    schedule = await tariff.price_schedule(timedelta(hours=12))
    assert all(pt.price_eur_per_kwh == 0.22 for pt in schedule)


async def test_price_schedule_ascending_timestamps() -> None:
    tariff = FlatRateTariff("default", 0.28)
    schedule = await tariff.price_schedule(timedelta(hours=6))
    timestamps = [pt.timestamp for pt in schedule]
    assert timestamps == sorted(timestamps)


async def test_price_schedule_hourly_increments() -> None:
    tariff = FlatRateTariff("default", 0.28)
    schedule = await tariff.price_schedule(timedelta(hours=4))
    for i in range(1, len(schedule)):
        delta = schedule[i].timestamp - schedule[i - 1].timestamp
        assert delta == timedelta(hours=1)


def test_negative_price_raises() -> None:
    with pytest.raises(ValueError):
        FlatRateTariff("default", -0.01)


def test_zero_price_is_valid() -> None:
    tariff = FlatRateTariff("feed_in", 0.0)
    assert tariff.price_eur_per_kwh == 0.0

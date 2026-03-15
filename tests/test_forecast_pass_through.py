"""Tests for PassThroughForecast."""

from __future__ import annotations

from datetime import timedelta

import pytest

from energy_manager.plugins.pass_through.forecast import PassThroughForecast
from energy_manager.core.forecast import ForecastProvider
from energy_manager.core.models import ForecastQuantity


def test_satisfies_protocol() -> None:
    stub = PassThroughForecast(ForecastQuantity.PV_GENERATION)
    assert isinstance(stub, ForecastProvider)


def test_quantity_property() -> None:
    for q in ForecastQuantity:
        stub = PassThroughForecast(q)
        assert stub.quantity == q


async def test_all_values_are_zero() -> None:
    stub = PassThroughForecast(ForecastQuantity.PV_GENERATION)
    points = await stub.get_forecast(timedelta(hours=24))
    assert all(pt.value == 0.0 for pt in points)


async def test_hourly_interval_24h() -> None:
    stub = PassThroughForecast(ForecastQuantity.PV_GENERATION)
    points = await stub.get_forecast(timedelta(hours=24))
    assert len(points) == 24


async def test_hourly_interval_6h() -> None:
    stub = PassThroughForecast(ForecastQuantity.CONSUMPTION)
    points = await stub.get_forecast(timedelta(hours=6))
    assert len(points) == 6


async def test_custom_interval() -> None:
    stub = PassThroughForecast(
        ForecastQuantity.PRICE,
        interval=timedelta(minutes=15),
    )
    points = await stub.get_forecast(timedelta(hours=1))
    assert len(points) == 4


async def test_ascending_timestamps() -> None:
    stub = PassThroughForecast(ForecastQuantity.PV_GENERATION)
    points = await stub.get_forecast(timedelta(hours=6))
    timestamps = [pt.timestamp for pt in points]
    assert timestamps == sorted(timestamps)


async def test_consecutive_timestamps_spaced_by_interval() -> None:
    stub = PassThroughForecast(ForecastQuantity.PV_GENERATION)
    points = await stub.get_forecast(timedelta(hours=4))
    for i in range(1, len(points)):
        assert points[i].timestamp - points[i - 1].timestamp == timedelta(hours=1)

"""Tests for LearnedConsumptionForecast and the learned_consumption plugin factory."""

from __future__ import annotations

from datetime import timedelta

import pytest

from energy_assistant.core.learned_model import LearnedConsumptionModel
from energy_assistant.core.learned_model_store import LearnedModelConfig, LearnedModelStore
from energy_assistant.core.models import ForecastQuantity
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.plugins import registry as plugin_registry
from energy_assistant.plugins.learned_consumption.forecast import LearnedConsumptionForecast
from helpers.fake_ha_client import FakeHAClient


# ---------------------------------------------------------------------------
# quantity + basic forecast generation
# ---------------------------------------------------------------------------


def test_quantity():
    fc = LearnedConsumptionForecast(
        device_id="heatpump_meter", model_store=None, ha_client=None, environment={}
    )
    assert fc.quantity == ForecastQuantity.CONSUMPTION


@pytest.mark.asyncio
async def test_get_forecast_without_model_returns_zero_points():
    fc = LearnedConsumptionForecast(
        device_id="heatpump_meter", model_store=LearnedModelStore(), ha_client=None, environment={}
    )
    points = await fc.get_forecast(timedelta(hours=24))
    assert len(points) >= 25
    assert all(p.value == 0.0 for p in points)


@pytest.mark.asyncio
async def test_get_forecast_uses_fitted_model_and_converts_to_kw():
    store = LearnedModelStore()
    model = LearnedConsumptionModel(min_samples_per_bucket=1)
    # A single sample gives every bucket a constant 500 W prediction.
    from datetime import datetime, timezone

    ts = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    model.fit([(ts, 10.0, 500.0, True)])
    store.set("heatpump_meter", model)

    fc = LearnedConsumptionForecast(
        device_id="heatpump_meter", model_store=store, ha_client=None, environment={}
    )
    points = await fc.get_forecast(timedelta(hours=6))
    assert all(p.value == pytest.approx(0.5) for p in points)  # 500 W -> 0.5 kW


@pytest.mark.asyncio
async def test_get_forecast_timestamps_increasing():
    fc = LearnedConsumptionForecast(
        device_id="heatpump_meter", model_store=LearnedModelStore(), ha_client=None, environment={}
    )
    points = await fc.get_forecast(timedelta(hours=12))
    for i in range(1, len(points)):
        assert points[i].timestamp > points[i - 1].timestamp


# ---------------------------------------------------------------------------
# Weather forecast fetching / temperature matching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_weather_forecast_no_entity_configured_returns_empty():
    fc = LearnedConsumptionForecast(
        device_id="d", model_store=None, ha_client=FakeHAClient(), environment={}
    )
    assert await fc._fetch_weather_forecast() == []


@pytest.mark.asyncio
async def test_fetch_weather_forecast_parses_points():
    client = FakeHAClient(
        forecasts={
            "weather.home": [
                {"datetime": "2026-07-06T10:00:00+00:00", "temperature": 18.5},
                {"datetime": "2026-07-06T11:00:00+00:00", "temperature": 19.0},
            ]
        }
    )
    fc = LearnedConsumptionForecast(
        device_id="d", model_store=None, ha_client=client, environment={"weather": "weather.home"}
    )
    points = await fc._fetch_weather_forecast()
    assert len(points) == 2
    assert points[0][1] == pytest.approx(18.5)


def test_temperature_at_falls_back_to_default_without_points():
    fc = LearnedConsumptionForecast(device_id="d", model_store=None, ha_client=None, environment={})
    from datetime import datetime, timezone

    assert fc._temperature_at(datetime.now(timezone.utc), []) == 15.0


def test_temperature_at_picks_nearest_point():
    from datetime import datetime, timedelta as td, timezone

    fc = LearnedConsumptionForecast(device_id="d", model_store=None, ha_client=None, environment={})
    base = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    points = [(base, 10.0), (base + td(hours=1), 20.0)]
    assert fc._temperature_at(base + td(minutes=10), points) == pytest.approx(10.0)
    assert fc._temperature_at(base + td(minutes=50), points) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Plugin factory registration
# ---------------------------------------------------------------------------


def test_plugin_registered():
    assert "learned_consumption" in plugin_registry._forecast


def test_build_registers_device_in_model_store():
    store = LearnedModelStore()
    ctx = BuildContext(backends=None, learned_model_store=store, environment={})
    provider = plugin_registry.build_forecast(
        "climate_all_forecast",
        {
            "type": "learned_consumption", "history_days": 30, "min_samples_per_bucket": 5,
            "_device_id": "climate_all",
        },
        ctx,
    )
    assert isinstance(provider, LearnedConsumptionForecast)
    config = store.get_config("climate_all")
    assert config == LearnedModelConfig(history_days=30, min_samples_per_bucket=5)


def test_build_uses_device_id_from_cfg_not_forecast_id():
    """Regression test: server._build_tariff_weighted_price_forecast calls us
    with forecast_id == f"{device_id}_weighted_price_forecast" — a different
    suffix than build_device_forecasts' f"{device_id}_forecast". Parsing
    forecast_id would silently derive the wrong device_id in that case."""
    store = LearnedModelStore()
    ctx = BuildContext(backends=None, learned_model_store=store, environment={})
    provider = plugin_registry.build_forecast(
        "climate_all_weighted_price_forecast",
        {"type": "learned_consumption", "_device_id": "climate_all"},
        ctx,
    )
    assert provider._device_id == "climate_all"
    assert store.get_config("climate_all") is not None
    assert store.get_config("climate_all_weighted_price") is None


def test_build_falls_back_and_warns_when_device_id_missing(caplog):
    store = LearnedModelStore()
    ctx = BuildContext(backends=None, learned_model_store=store, environment={})
    with caplog.at_level("WARNING"):
        provider = plugin_registry.build_forecast(
            "climate_all_forecast", {"type": "learned_consumption"}, ctx
        )
    assert provider._device_id == "climate_all"
    assert "falling back" in caplog.text

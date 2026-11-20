"""Tests for the per-zone price inputs the server hands to the optimizer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from energy_assistant.core.config import AppConfig
from energy_assistant.core.device_model import DeviceCategory, parse_devices
from energy_assistant.core.models import ForecastPoint, ForecastQuantity, TariffPoint
from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
from energy_assistant.server import Application

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
SPOT = [0.33, 0.42, 0.40, 0.37, 0.30, 0.36]


class _SpotTariff:
    tariff_id = "household"

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        return [TariffPoint(timestamp=NOW + timedelta(hours=h), price_eur_per_kwh=p)
                for h, p in enumerate(SPOT)]


class _Forecast:
    quantity = ForecastQuantity.CONSUMPTION

    def __init__(self, kw: float) -> None:
        self._kw = kw
        self.calls = 0

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        self.calls += 1
        return [ForecastPoint(timestamp=NOW + timedelta(hours=h), value=self._kw)
                for h in range(len(SPOT))]


def _app(devices: dict[str, dict], providers: dict[str, _Forecast],
         default_tariff: str | None = "household") -> Application:
    app = Application.__new__(Application)
    app._cfg = AppConfig(devices=parse_devices(devices), default_tariff_id=default_tariff)
    app._tariffs = {
        "household": _SpotTariff(),
        "heatpump": FlatRateTariff("heatpump", import_price_eur_per_kwh=0.178),
    }
    app._registry = SimpleNamespace(
        get=lambda device_id: SimpleNamespace(category=DeviceCategory.CONSUMER)
        if device_id in devices else None
    )
    app._horizon = timedelta(hours=len(SPOT) - 1)
    app._device_forecast_providers = providers
    app._build_ctx = None
    return app


_FC = {"type": "static_profile", "profile": {}}


async def test_primary_price_is_the_default_tariff_not_a_load_weighted_blend() -> None:
    """Regression: a big heat pump load on a cheap flat tariff used to drag
    the optimizer's price towards that tariff, hiding the spot price spread."""
    providers = {"baseline": _Forecast(0.5), "heatpump_meter": _Forecast(3.0)}
    app = _app(
        {"baseline": {"category": "consumer", "type": "generic_consumer", "forecast": _FC},
         "heatpump_meter": {"category": "consumer", "type": "generic_consumer", "tariff": "heatpump", "forecast": _FC}},
        providers,
    )
    prices, estimated, secondary = await app._build_tariff_zone_inputs()

    by_ts = {p.timestamp: p.value for p in prices}
    for h, spot in enumerate(SPOT):
        assert by_ts[NOW + timedelta(hours=h)] == pytest.approx(spot)
    assert set(estimated) == set(by_ts)

    (hp,) = secondary
    assert hp.tariff_id == "heatpump"
    assert [p.value for p in hp.load] == [3.0] * len(SPOT)
    assert {p.value for p in hp.prices} == {0.178}


async def test_reuses_the_planning_forecast_instance() -> None:
    providers = {"heatpump_meter": _Forecast(3.0)}
    app = _app(
        {"heatpump_meter": {"category": "consumer", "type": "generic_consumer", "tariff": "heatpump", "forecast": _FC}},
        providers,
    )
    await app._build_tariff_zone_inputs()
    assert providers["heatpump_meter"].calls == 1


async def test_consumers_on_the_default_tariff_are_not_secondary() -> None:
    app = _app(
        {"baseline": {"category": "consumer", "type": "generic_consumer", "forecast": _FC},
         "climate": {"category": "consumer", "type": "generic_consumer", "tariff": "household", "forecast": _FC}},
        {"baseline": _Forecast(0.5), "climate": _Forecast(0.3)},
    )
    prices, _, secondary = await app._build_tariff_zone_inputs()
    assert prices and secondary == []


async def test_no_default_tariff_yields_nothing() -> None:
    app = _app(
        {"heatpump_meter": {"category": "consumer", "type": "generic_consumer", "tariff": "heatpump", "forecast": _FC}},
        {"heatpump_meter": _Forecast(3.0)},
        default_tariff=None,
    )
    assert await app._build_tariff_zone_inputs() == ([], {}, [])

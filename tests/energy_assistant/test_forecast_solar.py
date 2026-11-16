"""Tests for the native Forecast.Solar PV forecast plugin."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from energy_assistant.core.models import ForecastQuantity
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.plugins import registry
from energy_assistant.plugins.forecast_solar import forecast as fs
from energy_assistant.plugins.forecast_solar.forecast import (
    ForecastSolarForecast,
    Plane,
    parse_response,
)


def _body(periods: dict[str, float], tz: str = "Europe/Berlin") -> dict:
    return {
        "result": {"watts": {k: 0 for k in periods}, "watt_hours_period": periods},
        "message": {"code": 0, "type": "success", "text": "", "info": {"timezone": tz}},
    }


# Two days, sunrise/sunset at odd minutes like the real API (CET = UTC+1).
_PERIODS = {
    "2026-11-16 07:30:00": 0,
    "2026-11-16 08:00:00": 50,    # 0.5 h → 0.1 kW from 07:30
    "2026-11-16 09:00:00": 300,   # 1 h   → 0.3 kW from 08:00
    "2026-11-16 09:15:00": 25,    # 0.25 h → 0.1 kW from 09:00
    "2026-11-17 07:30:00": 0,
    "2026-11-17 08:00:00": 100,
}


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_parse_response_period_average_at_period_start() -> None:
    pts = [(p.timestamp, round(p.value, 6)) for p in parse_response(_body(_PERIODS))]
    assert pts == [
        (_utc("2026-11-16T06:30:00"), 0.1),
        (_utc("2026-11-16T07:00:00"), 0.3),
        (_utc("2026-11-16T08:00:00"), 0.1),
        (_utc("2026-11-16T08:15:00"), 0.0),   # sunset
        (_utc("2026-11-17T06:30:00"), 0.2),
        (_utc("2026-11-17T07:00:00"), 0.0),
    ]


def test_energy_is_preserved() -> None:
    pts = parse_response(_body(_PERIODS))
    day1 = [p for p in pts if p.timestamp.date().isoformat() == "2026-11-16"]
    wh = sum(a.value * (b.timestamp - a.timestamp).total_seconds() / 3.6
             for a, b in zip(day1, day1[1:]))
    assert wh == pytest.approx(375)


def test_parse_response_falls_back_to_watts() -> None:
    body = {"result": {"watts": {"2026-11-16 12:00:00": 1500}},
            "message": {"code": 0, "info": {"timezone": "UTC"}}}
    [p] = parse_response(body)
    assert p.timestamp == _utc("2026-11-16T12:00:00") and p.value == 1.5


def test_sum_steps_aligns_different_breakpoints() -> None:
    from energy_assistant.core.models import ForecastPoint as P
    a = [P(timestamp=_utc("2026-11-16T07:00"), value=1.0), P(timestamp=_utc("2026-11-16T09:00"), value=0.0)]
    b = [P(timestamp=_utc("2026-11-16T08:00"), value=2.0), P(timestamp=_utc("2026-11-16T10:00"), value=0.0)]
    out = [(p.timestamp.hour, p.value) for p in fs._sum_steps([a, b])]
    assert out == [(7, 1.0), (8, 3.0), (9, 2.0), (10, 0.0)]


class _Server:
    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return self._responses[min(len(self.urls) - 1, len(self._responses) - 1)]


def _provider(server: _Server, planes: list[Plane], **kw) -> ForecastSolarForecast:
    return ForecastSolarForecast(
        "pv", 52.5, 6.4, planes,
        http=httpx.AsyncClient(transport=httpx.MockTransport(server)), **kw,
    )


def _future_periods(hours: int = 6) -> dict[str, float]:
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return {(start + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"): 1000.0 for i in range(hours)}


async def test_get_forecast_caches_and_filters_horizon() -> None:
    server = _Server(httpx.Response(200, json=_body(_future_periods(), tz="UTC")))
    prov = _provider(server, [Plane(20, 50, 7.4)], damping_morning=0.2)
    assert prov.quantity == ForecastQuantity.PV_GENERATION

    pts = await prov.get_forecast(timedelta(hours=2))
    now = datetime.now(timezone.utc)
    assert pts[0].timestamp <= now < pts[1].timestamp           # current point kept
    assert all(p.timestamp <= now + timedelta(hours=2) for p in pts)
    assert pts[0].value == pytest.approx(1.0)

    await prov.get_forecast(timedelta(hours=2))
    assert len(server.urls) == 1                                  # cached
    assert server.urls[0].startswith("https://api.forecast.solar/estimate/52.5/6.4/20/50/7.4")
    assert "damping=0.2%2C0" in server.urls[0]


async def test_multiple_planes_are_summed_and_api_key_used() -> None:
    server = _Server(httpx.Response(200, json=_body(_future_periods(), tz="UTC")))
    prov = _provider(server, [Plane(20, -90, 3), Plane(20, 90, 3)], api_key="KEY")
    pts = await prov.get_forecast(timedelta(hours=3))
    assert pts[0].value == pytest.approx(2.0)
    assert len(server.urls) == 2
    assert all(u.startswith("https://api.forecast.solar/KEY/estimate/") for u in server.urls)


async def test_errors_keep_stale_data_and_back_off(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(fs.time, "monotonic", lambda: clock[0])
    server = _Server(
        httpx.Response(200, json=_body(_future_periods(), tz="UTC")),
        httpx.Response(429, json={"message": {"code": 429, "text": "Rate limit"}}),
    )
    prov = _provider(server, [Plane(20, 0, 5)], refresh_s=3600)
    assert await prov.get_forecast(timedelta(hours=2))

    clock[0] += 3601
    assert await prov.get_forecast(timedelta(hours=2))            # 429 → stale data
    assert len(server.urls) == 2
    clock[0] += 60
    await prov.get_forecast(timedelta(hours=2))
    assert len(server.urls) == 2                                  # backing off


async def test_no_data_returns_empty() -> None:
    server = _Server(httpx.Response(500, text="down"))
    prov = _provider(server, [Plane(20, 0, 5)])
    assert await prov.get_forecast(timedelta(hours=24)) == []


def test_build_from_config() -> None:
    ctx = BuildContext(backends=None)  # type: ignore[arg-type]
    prov = registry.build_forecast("pv", {
        "type": "forecast_solar", "latitude": "52,5176", "longitude": 13.43,
        "declination": 20, "azimuth": 50, "kwp": 7.4,
    }, ctx)
    assert isinstance(prov, ForecastSolarForecast)
    assert prov._planes == [Plane(20, 50, 7.4)]
    assert prov._latitude == pytest.approx(52.5176)

    assert registry.build_forecast("pv", {"type": "forecast_solar", "kwp": 5}, ctx) is None
    assert registry.build_forecast("pv", {
        "type": "forecast_solar", "latitude": 1, "longitude": 2, "planes": [{"kwp": 5}],
    }, ctx) is None

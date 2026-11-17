"""Tests for the native Tibber plugin (tariff, price cache, live meter)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from energy_assistant.core.models import DeviceRole
from energy_assistant.plugins.tibber import api as tibber_api
from energy_assistant.plugins.tibber.api import LiveMeasurement, TibberApi, _parse_live
from energy_assistant.plugins.tibber.device import TibberDevice
from energy_assistant.plugins.tibber.tariff import TibberTariff


def _slots(day: str, prices: list[float], step_min: int = 15) -> list[dict]:
    start = datetime.fromisoformat(f"{day}T00:00:00+01:00")
    return [
        {"startsAt": (start + timedelta(minutes=i * step_min)).isoformat(), "total": p}
        for i, p in enumerate(prices)
    ]


def _response(today: list[dict], tomorrow: list[dict], home_id: str = "home-a") -> dict:
    return {
        "data": {
            "viewer": {
                "websocketSubscriptionUrl": "wss://example.invalid/subscriptions",
                "homes": [
                    {
                        "id": home_id,
                        "timeZone": "Europe/Berlin",
                        "features": {"realTimeConsumptionEnabled": True},
                        "currentSubscription": {"priceInfo": {"today": today, "tomorrow": tomorrow}},
                    }
                ],
            }
        }
    }


class _Server:
    """httpx MockTransport handler that counts requests."""

    def __init__(self, *bodies: dict | int) -> None:
        self._bodies = list(bodies)
        self.calls = 0
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.queries.append(json.loads(request.content)["query"])
        body = self._bodies[min(self.calls - 1, len(self._bodies) - 1)]
        if isinstance(body, int):
            return httpx.Response(body, text="boom")
        return httpx.Response(200, json=body)


def _api(server: _Server, resolution: str = "QUARTER_HOURLY") -> TibberApi:
    return TibberApi("tok", resolution=resolution, http=httpx.AsyncClient(transport=httpx.MockTransport(server)))


async def test_price_schedule_merges_today_and_tomorrow() -> None:
    server = _Server(_response(_slots("2026-11-15", [0.30, 0.31]), _slots("2026-11-16", [0.20])))
    tariff = TibberTariff("household", _api(server))

    schedule = await tariff.price_schedule(timedelta(hours=48))

    assert [p.price_eur_per_kwh for p in schedule] == pytest.approx([0.30, 0.31, 0.20])
    assert schedule[0].timestamp == datetime(2026, 11, 14, 23, 0, tzinfo=timezone.utc)
    assert "priceInfo(resolution: QUARTER_HOURLY)" in server.queries[0]


async def test_prices_are_cached_between_calls() -> None:
    server = _Server(_response(_slots("2026-11-15", [0.30]), _slots("2026-11-16", [0.20])))
    tariff = TibberTariff("household", _api(server))

    await tariff.price_schedule(timedelta(hours=24))
    await tariff.price_schedule(timedelta(hours=24))

    assert server.calls == 1


async def test_refetches_once_cache_expires() -> None:
    server = _Server(
        _response(_slots("2026-11-15", [0.30]), []),
        _response(_slots("2026-11-15", [0.30]), _slots("2026-11-16", [0.20])),
    )
    api = _api(server)
    tariff = TibberTariff("household", api)

    first = await tariff.price_schedule(timedelta(hours=24))
    api._next_refresh = time.monotonic() - 1  # expire the cache
    second = await tariff.price_schedule(timedelta(hours=24))

    assert len(first) == 1 and len(second) == 2


def test_short_ttl_while_tomorrow_missing_in_the_afternoon(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(_Server(_response([], [])))
    api._prices = {"home-a": tibber_api.HomePrices(points=[], has_tomorrow=False, tz=tibber_api.ZoneInfo("Europe/Berlin"))}

    class _Afternoon(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return datetime(2026, 11, 15, 14, 0, tzinfo=tz)

    class _Morning(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return datetime(2026, 11, 15, 9, 0, tzinfo=tz)

    monkeypatch.setattr(tibber_api, "datetime", _Afternoon)
    assert api._ttl() == tibber_api._PRICE_TTL_WAITING_S
    monkeypatch.setattr(tibber_api, "datetime", _Morning)
    assert api._ttl() == tibber_api._PRICE_TTL_S


async def test_failed_refresh_keeps_cached_prices() -> None:
    server = _Server(_response(_slots("2026-11-15", [0.30]), []), 503)
    api = _api(server)
    tariff = TibberTariff("household", api)

    await tariff.price_schedule(timedelta(hours=24))
    api._next_refresh = time.monotonic() - 1
    schedule = await tariff.price_schedule(timedelta(hours=24))

    assert server.calls == 2
    assert [p.price_eur_per_kwh for p in schedule] == pytest.approx([0.30])


async def test_no_prices_returns_zero_schedule() -> None:
    tariff = TibberTariff("household", _api(_Server(503)))

    schedule = await tariff.price_schedule(timedelta(hours=2))

    assert schedule and all(p.price_eur_per_kwh == 0.0 for p in schedule)


async def test_price_at_picks_the_quarter_hour_slot() -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    slot = now.replace(minute=now.minute - now.minute % 15)
    today = [
        {"startsAt": (slot + timedelta(minutes=15 * i)).isoformat(), "total": p}
        for i, p in enumerate([0.40, 0.50, 0.60])
    ]
    tariff = TibberTariff("household", _api(_Server(_response(today, []))))

    assert await tariff.price_at(slot + timedelta(minutes=20)) == pytest.approx(0.50)
    assert await tariff.price_at(slot + timedelta(minutes=44)) == pytest.approx(0.60)
    assert await tariff.price_at(slot + timedelta(hours=5)) == 0.0


async def test_unknown_home_id_yields_no_prices() -> None:
    tariff = TibberTariff("household", _api(_Server(_response(_slots("2026-11-15", [0.3]), []))), "other")

    schedule = await tariff.price_schedule(timedelta(hours=1))

    assert all(p.price_eur_per_kwh == 0.0 for p in schedule)


def test_parse_live_measurement() -> None:
    m = _parse_live({"liveMeasurement": {
        "timestamp": "2026-11-15T14:51:11.000+01:00", "power": 0, "powerProduction": 850,
        "accumulatedConsumption": 6.1, "lastMeterConsumption": 1234.5, "lastMeterProduction": None,
    }})
    assert m is not None
    assert m.power_w == 0 and m.production_w == 850
    assert m.meter_consumption_kwh == 1234.5 and m.meter_production_kwh is None
    assert m.timestamp == datetime(2026, 11, 15, 13, 51, 11, tzinfo=timezone.utc)
    assert m.timestamp.utcoffset() == timedelta(0)
    assert _parse_live({"liveMeasurement": {"power": None}}) is None


class _FakeStream:
    def __init__(self, m: LiveMeasurement | None) -> None:
        self._m = m
        self.started = False

    def ensure_started(self) -> None:
        self.started = True

    def latest(self) -> LiveMeasurement | None:
        return self._m


async def test_device_reports_net_power() -> None:
    m = LiveMeasurement(
        timestamp=datetime.now(timezone.utc), power_w=200.0, production_w=1200.0,
        accumulated_consumption_kwh=3.0, meter_consumption_kwh=1000.0, meter_production_kwh=50.0,
    )
    stream = _FakeStream(m)
    device = TibberDevice("household_meter", DeviceRole.METER, stream)  # type: ignore[arg-type]

    state = await device.get_state()

    assert stream.started
    assert state.available and state.power_w == pytest.approx(-1000.0)
    assert state.energy_kwh == 1000.0
    assert state.extra["import_w"] == 200.0 and state.extra["export_w"] == 1200.0


async def test_device_unavailable_without_measurement() -> None:
    device = TibberDevice("household_meter", DeviceRole.METER, _FakeStream(None))  # type: ignore[arg-type]

    state = await device.get_state()

    assert not state.available and state.power_w is None


def test_registry_builds_tibber_tariff_and_device() -> None:
    from energy_assistant.plugins import registry

    assert "tibber" in registry._tariff
    assert "tibber" in registry._device


async def test_live_backoff_grows_without_data_and_resets_after_data(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from energy_assistant.plugins.tibber.api import LiveStream

    stream = LiveStream(_api(_Server(_response([], []))), "home-a")
    measurement = LiveMeasurement(datetime.now(timezone.utc), 100.0, 0.0, None, None, None)
    # Three silent sessions, then one that delivers data and drops with an error.
    sessions = iter(["silent", "silent", "silent", "data", "silent"])

    async def fake_session() -> bool:
        if next(sessions) == "data":
            stream._store(measurement)
            raise ConnectionError("dropped")
        raise TimeoutError

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(stream, "_connect_and_listen", fake_session)
    monkeypatch.setattr(tibber_api.random, "uniform", lambda a, b: 1.0)
    monkeypatch.setattr(tibber_api.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await stream._run()

    assert delays == [10.0, 20.0, 40.0, 5.0, 10.0]

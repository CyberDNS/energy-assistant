"""Tests for the entsoe tariff plugin (A44 parsing, price formula, cache, config)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from textwrap import dedent
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from energy_assistant.config.yaml import YamlConfigLoader
from energy_assistant.core.models import TariffPoint
from energy_assistant.core.tariff import TariffModel
from energy_assistant.loader.config_validation import validate_sections
from energy_assistant.loader.device_loader import build
from energy_assistant.plugins.entsoe import api as entsoe_api
from energy_assistant.plugins.entsoe.api import (
    EntsoeApi,
    EntsoeApiError,
    SpotPrices,
    parse_a44,
    zone_eic,
)
from energy_assistant.plugins.entsoe.tariff import EntsoeTariff, ExportRule, Formula
from energy_assistant.plugins.time_of_use.schedule import parse_schedule
from energy_assistant.plugins.time_of_use.tariff import TimeOfUseTariff

BRUSSELS = ZoneInfo("Europe/Brussels")
NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _period(start: str, end: str, resolution: str, points: dict[int, float]) -> str:
    pts = "".join(
        f"<Point><position>{pos}</position><price.amount>{price}</price.amount></Point>"
        for pos, price in points.items()
    )
    return (f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{pts}</Period>")


def _doc(*periods: str) -> str:
    series = "".join(
        "<TimeSeries><mRID>1</mRID><businessType>A62</businessType>"
        "<in_Domain.mRID codingScheme=\"A01\">10Y1001A1001A82H</in_Domain.mRID>"
        "<currency_Unit.name>EUR</currency_Unit.name>"
        "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
        f"<curveType>A03</curveType>{p}</TimeSeries>"
        for p in periods
    )
    return (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<Publication_MarketDocument xmlns="{NS}"><mRID>x</mRID><type>A44</type>'
            f"{series}</Publication_MarketDocument>")


def _ack(code: str, text: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:'
            'acknowledgementdocument:7:0"><mRID>y</mRID>'
            f"<Reason><code>{code}</code><text>{text}</text></Reason>"
            "</Acknowledgement_MarketDocument>")


def _quarter_day(start: datetime, n: int, base: float = 100.0) -> str:
    """One market day of *n* quarter hours, price = base + position."""
    end = start + timedelta(minutes=15 * n)
    fmt = "%Y-%m-%dT%H:%MZ"
    return _period(start.strftime(fmt), end.strftime(fmt), "PT15M",
                   {i: base + i for i in range(1, n + 1)})


# ---------------------------------------------------------------------------
# A44 parsing


def test_quarter_hour_prices_in_eur_per_kwh() -> None:
    points = parse_a44(_doc(_quarter_day(_utc("2026-11-25T23:00"), 96)))
    assert len(points) == 96
    assert points[0] == TariffPoint(timestamp=_utc("2026-11-25T23:00"), price_eur_per_kwh=0.101)
    assert points[-1].timestamp == _utc("2026-11-26T22:45")
    assert points[-1].price_eur_per_kwh == pytest.approx(0.196)


def test_left_out_positions_repeat_the_previous_price() -> None:
    """Curve type A03: a position without a Point keeps the price before it."""
    doc = _doc(_period("2026-11-25T23:00Z", "2026-11-26T00:00Z", "PT15M",
                       {1: 80.0, 3: -5.5}))
    assert [p.price_eur_per_kwh for p in parse_a44(doc)] == pytest.approx(
        [0.08, 0.08, -0.0055, -0.0055])


def test_finer_resolution_wins_and_coarser_fills_other_days() -> None:
    hourly_day1 = _period("2026-11-25T23:00Z", "2026-11-26T23:00Z", "PT60M",
                          {i: 50.0 for i in range(1, 25)})
    hourly_day2 = _period("2026-11-26T23:00Z", "2026-11-27T23:00Z", "PT60M",
                          {i: 60.0 for i in range(1, 25)})
    quarter_day1 = _quarter_day(_utc("2026-11-25T23:00"), 96)
    points = parse_a44(_doc(hourly_day1, hourly_day2, quarter_day1))
    by_ts = {p.timestamp: p.price_eur_per_kwh for p in points}
    assert len(points) == 96 + 24
    assert by_ts[_utc("2026-11-26T00:15")] == pytest.approx(0.106)
    assert by_ts[_utc("2026-11-27T05:00")] == pytest.approx(0.06)


@pytest.mark.parametrize(("start", "quarters"), [
    ("2026-03-28T23:00", 92),   # spring forward: 23-hour market day
    ("2026-10-24T22:00", 100),  # fall back: 25-hour market day
])
def test_dst_market_days(start: str, quarters: int) -> None:
    first = _utc(start)
    points = parse_a44(_doc(_quarter_day(first, quarters)))
    assert len(points) == quarters
    local_days = {p.timestamp.astimezone(BRUSSELS).date() for p in points}
    assert len(local_days) == 1
    assert points[-1].timestamp + timedelta(minutes=15) - first == timedelta(
        minutes=15 * quarters)


def test_no_data_is_empty_other_acknowledgements_raise() -> None:
    assert parse_a44(_ack("999", "No matching data found")) == []
    with pytest.raises(EntsoeApiError, match="refused"):
        parse_a44(_ack("998", "Invalid parameter"))
    with pytest.raises(EntsoeApiError, match="unreadable"):
        parse_a44("<html>")


def test_zones() -> None:
    assert zone_eic("DE-LU") == "10Y1001A1001A82H"
    assert zone_eic("be") == "10YBE----------2"
    assert zone_eic("NL") == "10YNL----------L"
    assert zone_eic("AT") == "10YAT-APG------L"
    assert zone_eic("XX") is None


# ---------------------------------------------------------------------------
# Formula


def test_formula_vat_applies_to_the_whole_sum() -> None:
    f = Formula(markup_eur_per_kwh=0.025, grid_fee_eur_per_kwh=0.09,
                taxes_eur_per_kwh=0.04, vat=0.19)
    # 100 €/MWh spot: (0.100 + 0.025 + 0.09 + 0.04) × 1.19
    assert f.import_price(0.100) == pytest.approx(0.30345)
    # A negative spot price lowers the sum before VAT.
    assert f.import_price(-0.050) == pytest.approx(0.12495)
    # Without surcharges a negative spot price stays negative, VAT included.
    assert Formula(vat=0.19).import_price(-0.2) == pytest.approx(-0.238)
    assert Formula().import_price(0.1) == pytest.approx(0.1)


def test_export_rules() -> None:
    assert ExportRule().export_price(0.1) == 0.0
    assert ExportRule(type="spot", factor=0.9, offset_eur_per_kwh=-0.01).export_price(0.1) == \
        pytest.approx(0.08)
    assert ExportRule(type="fixed", price_eur_per_kwh=0.08).export_price(0.5) == 0.08


# ---------------------------------------------------------------------------
# Tariff


class _FakeApi:
    def __init__(self, points: list[TariffPoint] | None) -> None:
        self.points = points

    async def prices(self) -> SpotPrices | None:
        if self.points is None:
            return None
        return SpotPrices(points=self.points, end=None)


def _spot(start: datetime, prices: list[float], step_min: int = 15) -> list[TariffPoint]:
    return [TariffPoint(timestamp=start + timedelta(minutes=step_min * i), price_eur_per_kwh=p)
            for i, p in enumerate(prices)]


FORMULA = Formula(markup_eur_per_kwh=0.025, grid_fee_eur_per_kwh=0.09,
                  taxes_eur_per_kwh=0.04, vat=0.19)


async def test_tariff_schedule_and_lookup() -> None:
    start = _utc("2026-11-25T23:00")
    tariff = EntsoeTariff("household", _FakeApi(_spot(start, [0.1, -0.05])), FORMULA,
                          ExportRule(type="spot"))
    assert isinstance(tariff, TariffModel)
    sched = await tariff.price_schedule(timedelta(hours=24))
    assert [p.timestamp for p in sched] == [start, start + timedelta(minutes=15)]
    assert [p.price_eur_per_kwh for p in sched] == pytest.approx([0.30345, 0.12495])
    assert await tariff.price_at(start + timedelta(minutes=20)) == pytest.approx(0.12495)
    assert await tariff.price_at(start + timedelta(minutes=30)) == 0.0   # beyond the prices
    export = await tariff.export_price_schedule(timedelta(hours=24))
    assert [p.price_eur_per_kwh for p in export] == pytest.approx([0.1, -0.05])
    assert await tariff.export_price_at(start) == pytest.approx(0.1)


async def test_tariff_without_prices_returns_a_zero_schedule() -> None:
    tariff = EntsoeTariff("household", _FakeApi(None), FORMULA,
                          ExportRule(type="fixed", price_eur_per_kwh=0.08))
    sched = await tariff.price_schedule(timedelta(hours=3))
    assert sched and {p.price_eur_per_kwh for p in sched} == {0.0}
    # The fixed feed-in price does not depend on the spot prices.
    export = await tariff.export_price_schedule(timedelta(hours=3))
    assert {p.price_eur_per_kwh for p in export} == {0.08}


async def test_time_of_use_grid_fees_on_top() -> None:
    start = datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(UTC)
    spot = EntsoeTariff("spot", _FakeApi(_spot(start, [0.1] * 96)), Formula(vat=0.0))
    schedule, errors = parse_schedule({"periods": [
        {"name": "low", "price_eur_per_kwh": -0.05, "windows": [{"from": "00:00", "to": "06:00"}]},
        {"name": "standard", "price_eur_per_kwh": 0.0, "default": True},
    ]})
    assert schedule is not None, errors
    household = TimeOfUseTariff("household", schedule, base=spot)
    assert await household.price_at(start + timedelta(hours=1)) == pytest.approx(0.05)
    assert await household.price_at(start + timedelta(hours=12)) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# API client


class _Server:
    def __init__(self, *bodies: str | int) -> None:
        self._bodies = list(bodies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self._bodies[min(len(self.requests) - 1, len(self._bodies) - 1)]
        if isinstance(body, int):
            return httpx.Response(body, text="boom")
        return httpx.Response(200, text=body)


def _client(server: _Server) -> EntsoeApi:
    return EntsoeApi("tok", "10Y1001A1001A82H",
                     http=httpx.AsyncClient(transport=httpx.MockTransport(server)))


async def test_request_asks_for_today_and_tomorrow_in_utc() -> None:
    server = _Server(_doc(_quarter_day(_utc("2026-11-25T23:00"), 96)))
    points = await _client(server).fetch(_utc("2026-11-26T10:00"))
    q = {k: v[0] for k, v in parse_qs(urlparse(str(server.requests[0].url)).query).items()}
    assert q == {
        "securityToken": "tok", "documentType": "A44",
        "in_Domain": "10Y1001A1001A82H", "out_Domain": "10Y1001A1001A82H",
        "periodStart": "202611252300", "periodEnd": "202611272300",
    }
    assert len(points) == 96


async def test_fetch_drops_prices_outside_the_two_days() -> None:
    server = _Server(_doc(_quarter_day(_utc("2026-11-24T23:00"), 96),
                          _quarter_day(_utc("2026-11-25T23:00"), 96)))
    points = await _client(server).fetch(_utc("2026-11-26T10:00"))
    assert points[0].timestamp == _utc("2026-11-25T23:00") and len(points) == 96


async def test_bad_token() -> None:
    server = _Server(401)
    with pytest.raises(EntsoeApiError, match="token"):
        await _client(server).fetch(_utc("2026-11-26T10:00"))


def _today_doc() -> str:
    today = datetime.now(BRUSSELS).date()
    start = datetime.combine(today, datetime.min.time(), BRUSSELS).astimezone(UTC)
    n = round((datetime.combine(today + timedelta(days=1), datetime.min.time(), BRUSSELS)
               .astimezone(UTC) - start) / timedelta(minutes=15))
    return _doc(_quarter_day(start, n))


async def test_prices_are_cached_and_kept_on_errors(monkeypatch) -> None:
    server = _Server(_today_doc(), 503)
    api = _client(server)
    first = await api.prices()
    assert first is not None and first.points
    assert first.end == first.points[-1].timestamp + timedelta(minutes=15)
    await api.prices()
    assert len(server.requests) == 1                  # cached

    clock = time.monotonic() + 4000
    monkeypatch.setattr(entsoe_api.time, "monotonic", lambda: clock)
    again = await api.prices()
    assert len(server.requests) == 2                  # refreshed, failed
    assert again is first                             # stale prices kept
    await api.prices()
    assert len(server.requests) == 2                  # retry waits _PRICE_RETRY_S


def test_ttl_is_short_while_tomorrow_is_overdue(monkeypatch) -> None:
    api = EntsoeApi("tok", "x", http=httpx.AsyncClient())

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime(2026, 11, 26, 14, 0, tzinfo=BRUSSELS).astimezone(tz)

    monkeypatch.setattr(entsoe_api, "datetime", _Clock)
    today_end = datetime(2026, 11, 27, 0, 0, tzinfo=BRUSSELS)
    api._prices = SpotPrices(points=[], end=today_end)
    assert api._ttl() == entsoe_api._PRICE_TTL_WAITING_S
    api._prices = SpotPrices(points=[], end=today_end + timedelta(days=1))
    assert api._ttl() == entsoe_api._PRICE_TTL_S


# ---------------------------------------------------------------------------
# Config


def test_issue_example_loads(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(dedent("""\
        tariffs:
          household:
            type: entsoe
            zone: DE-LU
            token: abc
            formula:
              markup_eur_per_kwh: 0.025
              grid_fee_eur_per_kwh: 0.09
              taxes_eur_per_kwh: 0.04
              vat: 0.19
            export:
              type: spot
              factor: 1.0
            default: true
    """))
    cfg = YamlConfigLoader(config).load()
    _, tariffs, _ = build(cfg)
    tariff = tariffs["household"]
    assert isinstance(tariff, EntsoeTariff)
    assert tariff._formula == FORMULA
    assert tariff._export == ExportRule(type="spot")


def test_config_errors() -> None:
    errors = validate_sections({"tariffs": {
        "a": {"type": "entsoe", "zone": "XX", "token": "t"},
        "b": {"type": "entsoe", "zone": "BE", "token": "t", "formula": {"vat": 19}},
        "c": {"type": "entsoe", "zone": "NL", "token": "t", "export": {"type": "market"}},
        "d": {"type": "entsoe", "token": "t"},
    }})
    text = "\n".join(errors)
    assert "tariffs: a: zone" in text
    assert "tariffs: b: formula" in text and "vat" in text
    assert "tariffs: c: export" in text
    assert "'zone' is required" in text


async def test_current_feed_in_price_is_the_slot_in_force_now() -> None:
    """Spot schedules start at midnight; the PV opportunity price is now's."""
    from energy_assistant.server import _current_export_price

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    slot = now - timedelta(minutes=now.minute % 15)
    start = slot - timedelta(hours=2)
    prices = [0.05] * 8 + [0.12] + [0.07] * 8          # index 8 = the slot now
    tariff = EntsoeTariff("household", _FakeApi(_spot(start, prices)), FORMULA,
                          ExportRule(type="spot"))
    assert await _current_export_price({"household": tariff}) == pytest.approx(0.12)

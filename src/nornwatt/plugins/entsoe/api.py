"""ENTSO-E Transparency Platform client: day-ahead prices (document A44).

One ``EntsoeApi`` per (token, bidding zone) is shared by every tariff that
uses it (see ``get_api``), so prices are fetched once per cycle.

Request
-------
``GET <api_url>?securityToken=…&documentType=A44&in_Domain=<EIC>&out_Domain=<EIC>
&periodStart=yyyyMMddHHmm&periodEnd=yyyyMMddHHmm`` (UTC).  The answer is a
``Publication_MarketDocument`` with one or more ``TimeSeries``; each has
``Period`` elements with a ``timeInterval``, a ``resolution`` (``PT15M``,
``PT30M``, ``PT60M``) and ``Point``\\ s (``position``, ``price.amount`` in
EUR/MWh).  Positions left out repeat the previous price (curve type A03).
Where a zone publishes the same day in several resolutions, the finest one
wins.  No data for the period comes back as an ``Acknowledgement_MarketDocument``.

Cache
-----
Like the Tibber client: 60 min normally, 5 min while tomorrow's prices are
missing after ``_TOMORROW_EXPECTED_HOUR`` (the day-ahead auction closes at
12:00 CET, results follow around 13:00), 2 min after a failed request
(stale prices are kept meanwhile).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ...core.models import TariffPoint
from ...core.plugin_registry import SharedPool

_log = logging.getLogger(__name__)

API_URL = "https://web-api.tp.entsoe.eu/api"

#: The day-ahead market day (SDAC) is a CET day for every zone.
MARKET_TZ = ZoneInfo("Europe/Brussels")

_PRICE_TTL_S = 3600.0
_PRICE_TTL_WAITING_S = 300.0
_PRICE_RETRY_S = 120.0
_TOMORROW_EXPECTED_HOUR = 13      # market-time hour after which tomorrow should exist
_TOMORROW_WARN_HOUR = 15          # … after which a missing day is logged as a warning

#: Bidding zone → (EIC area code, readable name).
ZONES: dict[str, tuple[str, str]] = {
    "AT": ("10YAT-APG------L", "Austria"),
    "BE": ("10YBE----------2", "Belgium"),
    "BG": ("10YCA-BULGARIA-R", "Bulgaria"),
    "CH": ("10YCH-SWISSGRIDZ", "Switzerland"),
    "CZ": ("10YCZ-CEPS-----N", "Czechia"),
    "DE-LU": ("10Y1001A1001A82H", "Germany / Luxembourg"),
    "DK1": ("10YDK-1--------W", "Denmark West (DK1)"),
    "DK2": ("10YDK-2--------M", "Denmark East (DK2)"),
    "EE": ("10Y1001A1001A39I", "Estonia"),
    "ES": ("10YES-REE------0", "Spain"),
    "FI": ("10YFI-1--------U", "Finland"),
    "FR": ("10YFR-RTE------C", "France"),
    "GR": ("10YGR-HTSO-----Y", "Greece"),
    "HR": ("10YHR-HEP------M", "Croatia"),
    "HU": ("10YHU-MAVIR----U", "Hungary"),
    "IT-NORD": ("10Y1001A1001A73I", "Italy North"),
    "LT": ("10YLT-1001A0008Q", "Lithuania"),
    "LV": ("10YLV-1001A00074", "Latvia"),
    "NL": ("10YNL----------L", "Netherlands"),
    "NO1": ("10YNO-1--------2", "Norway Oslo (NO1)"),
    "NO2": ("10YNO-2--------T", "Norway Kristiansand (NO2)"),
    "NO3": ("10YNO-3--------J", "Norway Trondheim (NO3)"),
    "NO4": ("10YNO-4--------9", "Norway Tromsø (NO4)"),
    "NO5": ("10Y1001A1001A48H", "Norway Bergen (NO5)"),
    "PL": ("10YPL-AREA-----S", "Poland"),
    "PT": ("10YPT-REN------W", "Portugal"),
    "RO": ("10YRO-TEL------P", "Romania"),
    "SE1": ("10Y1001A1001A44P", "Sweden Luleå (SE1)"),
    "SE2": ("10Y1001A1001A45N", "Sweden Sundsvall (SE2)"),
    "SE3": ("10Y1001A1001A46L", "Sweden Stockholm (SE3)"),
    "SE4": ("10Y1001A1001A47J", "Sweden Malmö (SE4)"),
    "SI": ("10YSI-ELES-----O", "Slovenia"),
    "SK": ("10YSK-SEPS-----K", "Slovakia"),
}

def zone_eic(zone: str) -> str | None:
    """EIC code of the bidding *zone* (a key of ``ZONES``), or None."""
    entry = ZONES.get(zone.strip().upper())
    return entry[0] if entry else None


class EntsoeApiError(RuntimeError):
    """HTTP errors and unreadable answers."""


@dataclass
class SpotPrices:
    points: list[TariffPoint]       # EUR/kWh, ascending, one per market time unit
    end: datetime | None            # end of the last point


# ---------------------------------------------------------------------------
# XML


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element | None, name: str) -> str | None:
    c = _child(el, name) if el is not None else None
    return c.text.strip() if c is not None and c.text else None


def _parse_time(s: str) -> datetime:
    # "2026-11-25T23:00Z"
    return datetime.fromisoformat(s).astimezone(UTC)


_RESOLUTION = re.compile(r"^PT(\d+)M$|^P1D$")


def _parse_resolution(s: str) -> timedelta:
    m = _RESOLUTION.match(s)
    if not m:
        raise EntsoeApiError(f"unknown resolution {s!r}")
    return timedelta(days=1) if m.group(1) is None else timedelta(minutes=int(m.group(1)))


@dataclass
class _Segment:
    start: datetime
    end: datetime
    step: timedelta
    prices: dict[int, float]        # position (1-based) → EUR/MWh


def parse_a44(xml: str | bytes) -> list[TariffPoint]:
    """Day-ahead prices in a ``Publication_MarketDocument``, EUR/kWh.

    Returns ``[]`` for an ``Acknowledgement_MarketDocument`` with reason
    999 (no data for the period); raises for other acknowledgements.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise EntsoeApiError(f"unreadable answer: {exc}") from exc
    kind = _local(root.tag)
    if kind == "Acknowledgement_MarketDocument":
        reason = _child(root, "Reason")
        code, text = _text(reason, "code"), _text(reason, "text") or ""
        if code == "999":
            return []
        raise EntsoeApiError(f"ENTSO-E refused the request ({code}): {text[:200]}")
    if kind != "Publication_MarketDocument":
        raise EntsoeApiError(f"unexpected answer {kind!r}")

    segments: list[_Segment] = []
    for ts in _children(root, "TimeSeries"):
        for period in _children(ts, "Period"):
            interval = _child(period, "timeInterval")
            start_s, end_s = _text(interval, "start"), _text(interval, "end")
            res_s = _text(period, "resolution")
            if not (start_s and end_s and res_s):
                continue
            prices: dict[int, float] = {}
            for point in _children(period, "Point"):
                pos, amount = _text(point, "position"), _text(point, "price.amount")
                if pos is not None and amount is not None:
                    prices[int(pos)] = float(amount)
            if prices:
                segments.append(_Segment(_parse_time(start_s), _parse_time(end_s),
                                         _parse_resolution(res_s), prices))

    # Finest resolution first; a coarser segment only fills what is not covered.
    segments.sort(key=lambda s: (s.step, s.start))
    covered: list[tuple[datetime, datetime]] = []
    by_ts: dict[datetime, float] = {}
    for seg in segments:
        if any(a <= seg.start and seg.end <= b for a, b in covered):
            continue  # a finer resolution has this span already
        n =round((seg.end - seg.start) / seg.step)
        last: float | None = None
        for pos in range(1, n + 1):
            last = seg.prices.get(pos, last)   # A03: a missing position repeats
            if last is None:
                continue
            t = seg.start + (pos - 1) * seg.step
            if any(a <= t < b for a, b in covered):
                continue
            by_ts[t] = last / 1000.0
        covered.append((seg.start, seg.end))
    return [TariffPoint(timestamp=t, price_eur_per_kwh=p) for t, p in sorted(by_ts.items())]


# ---------------------------------------------------------------------------
# Client


class EntsoeApi:
    """Shared day-ahead price client for one token and bidding zone."""

    def __init__(
        self,
        token: str,
        eic: str,
        *,
        api_url: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._eic = eic
        self._api_url = api_url or API_URL
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._lock = asyncio.Lock()
        self._prices: SpotPrices | None = None
        self._next_refresh = 0.0
        self._warned_missing_day: date | None = None

    async def prices(self) -> SpotPrices | None:
        """Spot prices from the start of today (market time) on, cached."""
        await self._refresh_if_due()
        return self._prices

    async def _refresh_if_due(self) -> None:
        if time.monotonic() < self._next_refresh:
            return
        async with self._lock:
            if time.monotonic() < self._next_refresh:
                return
            try:
                points = await self.fetch(datetime.now(UTC))
            except Exception as exc:  # noqa: BLE001
                self._next_refresh = time.monotonic() + _PRICE_RETRY_S
                if self._prices:
                    _log.warning("ENTSO-E price refresh failed (%s) — keeping cached prices", exc)
                else:
                    _log.warning("ENTSO-E price fetch failed: %s", exc)
                return
            end = None
            if points:
                step = (points[-1].timestamp - points[-2].timestamp) if len(points) > 1 \
                    else timedelta(hours=1)
                end = points[-1].timestamp + step
            self._prices = SpotPrices(points=points, end=end)
            self._next_refresh = time.monotonic() + self._ttl()

    async def fetch(self, now: datetime) -> list[TariffPoint]:
        """Prices of today and tomorrow (market days around *now*)."""
        today = now.astimezone(MARKET_TZ).date()
        start = datetime.combine(today, datetime.min.time(), MARKET_TZ).astimezone(UTC)
        end = datetime.combine(today + timedelta(days=2), datetime.min.time(),
                               MARKET_TZ).astimezone(UTC)
        params: dict[str, Any] = {
            "securityToken": self._token,
            "documentType": "A44",
            "in_Domain": self._eic,
            "out_Domain": self._eic,
            "periodStart": start.strftime("%Y%m%d%H%M"),
            "periodEnd": end.strftime("%Y%m%d%H%M"),
        }
        resp = await self._http.get(self._api_url, params=params)
        if resp.status_code == 401:
            raise EntsoeApiError("ENTSO-E rejected the API token (HTTP 401)")
        if resp.status_code != 200 and "Acknowledgement_MarketDocument" not in resp.text:
            raise EntsoeApiError(f"ENTSO-E API HTTP {resp.status_code}: {resp.text[:200]}")
        points = parse_a44(resp.content)
        return [p for p in points if start <= p.timestamp < end]

    def _ttl(self) -> float:
        """Shorter cache lifetime while tomorrow's prices are overdue."""
        local = datetime.now(MARKET_TZ)
        tomorrow_end = datetime.combine(local.date() + timedelta(days=2), datetime.min.time(),
                                        MARKET_TZ)
        prices = self._prices
        if prices is not None and prices.end is not None and prices.end >= tomorrow_end:
            return _PRICE_TTL_S
        if local.hour < _TOMORROW_EXPECTED_HOUR:
            return _PRICE_TTL_S
        if local.hour >= _TOMORROW_WARN_HOUR and self._warned_missing_day != local.date():
            self._warned_missing_day = local.date()
            _log.warning(
                "ENTSO-E has no day-ahead prices for tomorrow yet (%s CET) — retrying every %d min",
                local.strftime("%H:%M"), int(_PRICE_TTL_WAITING_S / 60),
            )
        return _PRICE_TTL_WAITING_S

    async def close(self) -> None:
        await self._http.aclose()


_apis: SharedPool[tuple[str, str, str], EntsoeApi] = SharedPool(stop=lambda api: api.close())


def get_api(token: str, eic: str, api_url: str | None = None) -> EntsoeApi:
    key = (token, eic, api_url or API_URL)
    return _apis.get(key, lambda: EntsoeApi(token, eic, api_url=api_url))

"""ForecastSolarForecast — PV generation forecast from the Forecast.Solar API.

``GET https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}`` returns
(timestamps are local time of the plant, ``message.info.timezone``)::

    {"result": {
        "watts":             {"2026-11-16 07:29:59": 0, "2026-11-16 08:00:00": 175, ...},
        "watt_hours_period": {"2026-11-16 07:29:59": 0, "2026-11-16 08:00:00": 44, ...},
        ...},
     "message": {"code": 0, "info": {"timezone": "Europe/Berlin", ...}, ...}}

Each day starts at sunrise and ends at sunset; the points in between are
full hours.  ``watt_hours_period[t]`` is the energy produced in the period
that *ends* at ``t``.

The optimizer holds each forecast point until the next one, so every period
becomes one point at its **start** carrying the period's average power
(``Wh / hours``), plus a 0 kW point at sunset.  Energy per slot then matches
the forecast exactly, instead of being shifted by half an hour as with
instantaneous hourly values.

Fetching
--------
The public API allows 12 requests per hour per IP, so responses are cached:
one request per plane every ``refresh_s`` (default 60 min), 5 min retry after
an error and 15 min after a rate-limit answer.  Stale data is kept meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ...core.models import ForecastPoint, ForecastQuantity

_log = logging.getLogger(__name__)

API_URL = "https://api.forecast.solar"

_RETRY_S = 300.0
_RATE_LIMIT_RETRY_S = 900.0
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Plane:
    declination: float   # tilt, 0 = horizontal, 90 = vertical
    azimuth: float       # -180 = north, -90 = east, 0 = south, 90 = west
    kwp: float           # installed peak power


class ForecastSolarError(RuntimeError):
    """Raised for HTTP errors and non-success API messages."""


def _user_agent() -> str:
    try:
        return f"nornwatt/{version('nornwatt')}"
    except PackageNotFoundError:
        return "nornwatt"


def _fmt(x: float) -> str:
    return f"{x:g}"


def _parse_ts(raw: str, tz: ZoneInfo) -> datetime:
    return datetime.strptime(raw, _TS_FORMAT).replace(tzinfo=tz).astimezone(timezone.utc)


def parse_response(body: dict[str, Any]) -> list[ForecastPoint]:
    """Turn a Forecast.Solar ``estimate`` response into step points (kW, UTC).

    Each point's value holds until the next point.
    """
    result = body.get("result") or {}
    tz_name = ((body.get("message") or {}).get("info") or {}).get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        _log.warning("Forecast.Solar: unknown timezone %r — assuming UTC", tz_name)
        tz = ZoneInfo("UTC")

    periods = result.get("watt_hours_period")
    if not periods:
        # Fall back to instantaneous power when the energy series is missing.
        watts = result.get("watts") or {}
        return sorted(
            (ForecastPoint(timestamp=_parse_ts(k, tz), value=float(v) / 1000.0)
             for k, v in watts.items()),
            key=lambda p: p.timestamp,
        )

    # Group the period ends by local day: each day runs sunrise → sunset.
    days: dict[str, list[tuple[datetime, float]]] = {}
    for key, wh in periods.items():
        try:
            days.setdefault(key[:10], []).append((_parse_ts(key, tz), float(wh)))
        except (ValueError, TypeError):
            continue

    points: list[ForecastPoint] = []
    for day in sorted(days):
        entries = sorted(days[day])
        for (start, _), (end, wh) in zip(entries, entries[1:]):
            hours = (end - start).total_seconds() / 3600.0
            if hours <= 0:
                continue
            points.append(ForecastPoint(timestamp=start, value=max(0.0, wh) / hours / 1000.0))
        points.append(ForecastPoint(timestamp=entries[-1][0], value=0.0))  # sunset
    return points


def _sum_steps(series: list[list[ForecastPoint]]) -> list[ForecastPoint]:
    """Sum several step series (each value holds until that series' next point)."""
    series = [s for s in series if s]
    if len(series) == 1:
        return list(series[0])
    stamps = sorted({p.timestamp for s in series for p in s})
    idx = [0] * len(series)
    current = [0.0] * len(series)
    out: list[ForecastPoint] = []
    for ts in stamps:
        for i, s in enumerate(series):
            while idx[i] < len(s) and s[idx[i]].timestamp <= ts:
                current[i] = s[idx[i]].value
                idx[i] += 1
        out.append(ForecastPoint(timestamp=ts, value=sum(current)))
    return out


class ForecastSolarForecast:
    """``ForecastProvider`` for PV generation backed by Forecast.Solar."""

    def __init__(
        self,
        forecast_id: str,
        latitude: float,
        longitude: float,
        planes: list[Plane],
        *,
        damping_morning: float = 0.0,
        damping_evening: float = 0.0,
        api_key: str | None = None,
        api_url: str | None = None,
        refresh_s: float = 3600.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._forecast_id = forecast_id
        self._latitude = latitude
        self._longitude = longitude
        self._planes = list(planes)
        self._damping = (damping_morning, damping_evening)
        self._api_key = api_key
        self._api_url = (api_url or API_URL).rstrip("/")
        self._refresh_s = refresh_s
        self._http = http or httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": _user_agent(), "Accept": "application/json"}
        )
        self._lock = asyncio.Lock()
        self._cache: dict[Plane, list[ForecastPoint]] = {}
        self._next_refresh: dict[Plane, float] = {}

    @property
    def quantity(self) -> ForecastQuantity:
        return ForecastQuantity.PV_GENERATION

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _url(self, plane: Plane) -> str:
        prefix = f"{self._api_url}/{self._api_key}" if self._api_key else self._api_url
        return (
            f"{prefix}/estimate/{_fmt(self._latitude)}/{_fmt(self._longitude)}"
            f"/{_fmt(plane.declination)}/{_fmt(plane.azimuth)}/{_fmt(plane.kwp)}"
        )

    async def _fetch(self, plane: Plane) -> list[ForecastPoint]:
        params: dict[str, str] = {}
        if any(self._damping):
            params["damping"] = f"{_fmt(self._damping[0])},{_fmt(self._damping[1])}"
        resp = await self._http.get(self._url(plane), params=params)
        if resp.status_code == 429:
            raise _RateLimited()
        try:
            body = resp.json()
        except ValueError:
            body = {}
        message = body.get("message") or {}
        if resp.status_code != 200 or message.get("code", 0) != 0:
            text = message.get("text") or resp.text[:200]
            raise ForecastSolarError(f"HTTP {resp.status_code}: {text}")
        return parse_response(body)

    async def _refresh(self) -> None:
        async with self._lock:
            now = time.monotonic()
            for plane in self._planes:
                if now < self._next_refresh.get(plane, 0.0):
                    continue
                try:
                    points = await self._fetch(plane)
                except _RateLimited:
                    _log.warning("Forecast.Solar %r: rate limit reached — retrying in %.0f min",
                                 self._forecast_id, _RATE_LIMIT_RETRY_S / 60)
                    self._next_refresh[plane] = now + _RATE_LIMIT_RETRY_S
                    continue
                except Exception as exc:  # network errors, bad answers
                    _log.warning("Forecast.Solar %r: request failed (%s) — retrying in %.0f min",
                                 self._forecast_id, exc, _RETRY_S / 60)
                    self._next_refresh[plane] = now + _RETRY_S
                    continue
                self._cache[plane] = points
                self._next_refresh[plane] = now + self._refresh_s
                _log.info("Forecast.Solar %r: fetched %d points for %s", self._forecast_id,
                          len(points), plane)

    # ------------------------------------------------------------------
    # ForecastProvider
    # ------------------------------------------------------------------

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Return PV power (kW) step points from the current one to ``now + horizon``.

        Returns an empty list when no data has ever been fetched, so the
        optimizer falls back to zero PV.
        """
        await self._refresh()
        points = _sum_steps([self._cache.get(p, []) for p in self._planes])
        if not points:
            return []
        now = datetime.now(timezone.utc)
        cutoff = now + horizon
        # Keep the point that is in effect now (its timestamp is in the past).
        current = [p for p in points if p.timestamp <= now][-1:]
        return current + [p for p in points if now < p.timestamp <= cutoff]


class _RateLimited(Exception):
    pass

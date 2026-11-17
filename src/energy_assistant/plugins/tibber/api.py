"""Tibber API client — GraphQL price queries and the live-measurement stream.

One ``TibberApi`` per access token (see ``get_api``) is shared by every
tariff and device that uses that token, so prices are fetched once per cycle
and only one websocket is open per home.

Prices
------
``priceInfo { today tomorrow }`` is cached.  The cache lifetime adapts:

* 60 min normally,
* 5 min while tomorrow's prices are still missing after ``_TOMORROW_EXPECTED_HOUR``
  (Tibber publishes the day-ahead prices around 13:00 local time),
* 2 min retry after a failed request (stale data is kept meanwhile).

Live measurement
----------------
The Pulse/Watty stream uses the ``graphql-transport-ws`` protocol on the URL
reported by ``viewer.websocketSubscriptionUrl``.  The stream reconnects with
exponential backoff (Tibber asks clients not to hammer the endpoint) and is
restarted when no data has arrived for ``_LIVE_STALL_S``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from zoneinfo import ZoneInfo

import certifi
import httpx

from ...core.models import TariffPoint

_log = logging.getLogger(__name__)

API_URL = "https://api.tibber.com/v1-beta/gql"

_PRICE_TTL_S = 3600.0
_PRICE_TTL_WAITING_S = 300.0
_PRICE_RETRY_S = 120.0
_TOMORROW_EXPECTED_HOUR = 13      # local hour after which tomorrow should exist
_TOMORROW_WARN_HOUR = 15          # local hour after which a missing day is logged as a warning

_LIVE_STALE_S = 60.0              # measurements older than this are not reported
_LIVE_STALL_S = 120.0             # reconnect when the stream is silent this long
_LIVE_BACKOFF_MIN_S = 5.0
_LIVE_BACKOFF_MAX_S = 300.0

_HOMES_QUERY = """
{
  viewer {
    websocketSubscriptionUrl
    homes {
      id
      timeZone
      features { realTimeConsumptionEnabled }
      currentSubscription {
        priceInfo(resolution: %s) {
          today { total startsAt }
          tomorrow { total startsAt }
        }
      }
    }
  }
}
"""

_LIVE_SUBSCRIPTION = """
subscription {
  liveMeasurement(homeId: "%s") {
    timestamp
    power
    powerProduction
    accumulatedConsumption
    lastMeterConsumption
    lastMeterProduction
  }
}
"""


def _user_agent() -> str:
    try:
        return f"energy-assistant/{version('energy-assistant')}"
    except PackageNotFoundError:
        return "energy-assistant"


def _ssl_context() -> ssl.SSLContext:
    # certifi ships with httpx; using it for the websocket too avoids
    # depending on the system CA store (missing on python.org macOS builds).
    return ssl.create_default_context(cafile=certifi.where())


class TibberApiError(RuntimeError):
    """Raised for HTTP errors and GraphQL ``errors`` responses."""


@dataclass
class HomePrices:
    points: list[TariffPoint]
    has_tomorrow: bool
    tz: ZoneInfo


@dataclass
class LiveMeasurement:
    timestamp: datetime
    power_w: float               # consumption (import) in W
    production_w: float          # production (export) in W
    accumulated_consumption_kwh: float | None
    meter_consumption_kwh: float | None
    meter_production_kwh: float | None


def _parse_points(entries: list[dict[str, Any]] | None) -> list[TariffPoint]:
    points: list[TariffPoint] = []
    for entry in entries or []:
        starts_at = entry.get("startsAt")
        total = entry.get("total")
        if starts_at is None or total is None:
            continue
        ts = datetime.fromisoformat(starts_at)
        ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        points.append(TariffPoint(timestamp=ts, price_eur_per_kwh=float(total)))
    return points


def _parse_live(data: dict[str, Any]) -> LiveMeasurement | None:
    m = data.get("liveMeasurement")
    if not m or m.get("power") is None:
        return None
    # Tibber stamps live readings in local time ("+01:00"); keep them in UTC
    # like every other device timestamp.
    if m.get("timestamp"):
        ts = datetime.fromisoformat(m["timestamp"])
        ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)

    def _f(key: str) -> float | None:
        v = m.get(key)
        return float(v) if v is not None else None

    return LiveMeasurement(
        timestamp=ts,
        power_w=float(m["power"]),
        production_w=_f("powerProduction") or 0.0,
        accumulated_consumption_kwh=_f("accumulatedConsumption"),
        meter_consumption_kwh=_f("lastMeterConsumption"),
        meter_production_kwh=_f("lastMeterProduction"),
    )


class TibberApi:
    """Shared Tibber client for one access token."""

    def __init__(
        self,
        token: str,
        *,
        resolution: str = "QUARTER_HOURLY",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._resolution = resolution
        self._http = http or httpx.AsyncClient(
            timeout=20.0,
            headers={"Authorization": f"Bearer {token}", "User-Agent": _user_agent()},
        )
        self._lock = asyncio.Lock()
        self._prices: dict[str, HomePrices] = {}
        self._home_ids: list[str] = []
        self._realtime: dict[str, bool] = {}
        self._ws_url: str | None = None
        self._next_refresh = 0.0          # monotonic deadline for the next price fetch
        self._warned_missing_day: date | None = None
        self._streams: dict[str, LiveStream] = {}

    # ------------------------------------------------------------------
    # GraphQL
    # ------------------------------------------------------------------

    async def query(self, query: str) -> dict[str, Any]:
        resp = await self._http.post(API_URL, json={"query": query})
        if resp.status_code != 200:
            raise TibberApiError(f"Tibber API HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if body.get("errors"):
            msgs = "; ".join(str(e.get("message", e)) for e in body["errors"])
            raise TibberApiError(f"Tibber API error: {msgs}")
        return body.get("data") or {}

    # ------------------------------------------------------------------
    # Homes and prices
    # ------------------------------------------------------------------

    async def resolve_home_id(self, home_id: str | None) -> str:
        """Return *home_id*, or the account's first home when it is empty."""
        await self._refresh_if_due()
        if home_id:
            if self._home_ids and home_id not in self._home_ids:
                raise TibberApiError(
                    f"Tibber home {home_id!r} not found on this account "
                    f"(available: {', '.join(self._home_ids)})"
                )
            return home_id
        if not self._home_ids:
            raise TibberApiError("Tibber account has no homes")
        return self._home_ids[0]

    async def prices(self, home_id: str) -> HomePrices | None:
        """Today's and tomorrow's prices for *home_id* (cached)."""
        await self._refresh_if_due()
        return self._prices.get(home_id)

    async def websocket_url(self) -> str:
        await self._refresh_if_due()
        if not self._ws_url:
            raise TibberApiError("Tibber API returned no websocketSubscriptionUrl")
        return self._ws_url

    def realtime_enabled(self, home_id: str) -> bool | None:
        return self._realtime.get(home_id)

    async def _refresh_if_due(self) -> None:
        if time.monotonic() < self._next_refresh and self._home_ids:
            return
        async with self._lock:
            if time.monotonic() < self._next_refresh and self._home_ids:
                return
            try:
                await self._fetch()
            except Exception as exc:  # noqa: BLE001
                self._next_refresh = time.monotonic() + _PRICE_RETRY_S
                if self._prices:
                    _log.warning("Tibber price refresh failed (%s) — keeping cached prices", exc)
                else:
                    _log.warning("Tibber price fetch failed: %s", exc)

    async def _fetch(self) -> None:
        data = await self.query(_HOMES_QUERY % self._resolution)
        viewer = data.get("viewer") or {}
        self._ws_url = viewer.get("websocketSubscriptionUrl") or self._ws_url

        prices: dict[str, HomePrices] = {}
        home_ids: list[str] = []
        for home in viewer.get("homes") or []:
            hid = home.get("id")
            if not hid:
                continue
            home_ids.append(hid)
            self._realtime[hid] = bool((home.get("features") or {}).get("realTimeConsumptionEnabled"))
            tz = ZoneInfo(home.get("timeZone") or "Europe/Berlin")
            info = (home.get("currentSubscription") or {}).get("priceInfo") or {}
            tomorrow = _parse_points(info.get("tomorrow"))
            points = sorted(_parse_points(info.get("today")) + tomorrow, key=lambda p: p.timestamp)
            prices[hid] = HomePrices(points=points, has_tomorrow=bool(tomorrow), tz=tz)

        self._prices = prices
        self._home_ids = home_ids
        self._next_refresh = time.monotonic() + self._ttl()

    def _ttl(self) -> float:
        """Shorter cache lifetime while tomorrow's prices are overdue."""
        ttl = _PRICE_TTL_S
        for hp in self._prices.values():
            if hp.has_tomorrow:
                continue
            local = datetime.now(hp.tz)
            if local.hour >= _TOMORROW_EXPECTED_HOUR:
                ttl = _PRICE_TTL_WAITING_S
                if local.hour >= _TOMORROW_WARN_HOUR and self._warned_missing_day != local.date():
                    self._warned_missing_day = local.date()
                    _log.warning(
                        "Tibber has not published tomorrow's prices yet (%s local) — "
                        "retrying every %d min", local.strftime("%H:%M"), int(_PRICE_TTL_WAITING_S / 60),
                    )
        return ttl

    # ------------------------------------------------------------------
    # Live stream
    # ------------------------------------------------------------------

    def live_stream(self, home_id: str | None) -> LiveStream:
        key = home_id or ""
        stream = self._streams.get(key)
        if stream is None:
            stream = LiveStream(self, home_id)
            self._streams[key] = stream
        return stream

    async def close(self) -> None:
        for stream in self._streams.values():
            await stream.stop()
        await self._http.aclose()

    @property
    def token(self) -> str:
        return self._token


class LiveStream:
    """Background ``liveMeasurement`` subscription for one home."""

    def __init__(self, api: TibberApi, home_id: str | None) -> None:
        self._api = api
        self._home_id = home_id
        self._task: asyncio.Task[None] | None = None
        self._latest: LiveMeasurement | None = None
        self._latest_mono = 0.0
        self._session_got_data = False

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="tibber_live")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def latest(self, max_age_s: float = _LIVE_STALE_S) -> LiveMeasurement | None:
        if self._latest is None or time.monotonic() - self._latest_mono > max_age_s:
            return None
        return self._latest

    def _store(self, m: LiveMeasurement) -> None:
        self._latest = m
        self._latest_mono = time.monotonic()
        self._session_got_data = True

    async def _run(self) -> None:
        backoff = _LIVE_BACKOFF_MIN_S
        while True:
            # Set by _store — survives a session that ends with an exception,
            # so a healthy connection that drops resets the backoff.
            self._session_got_data = False
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _log.warning(
                    "Tibber live stream silent for %.0f s — reconnecting", _LIVE_STALL_S
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("Tibber live stream disconnected: %r", exc)
            got_data = self._session_got_data
            backoff = _LIVE_BACKOFF_MIN_S if got_data else min(backoff * 2, _LIVE_BACKOFF_MAX_S)
            delay = backoff * random.uniform(0.8, 1.2)
            _log.info("Tibber live stream reconnecting in %.0f s", delay)
            await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> bool:
        """Run one websocket session; return True when any data arrived."""
        from websockets.asyncio.client import connect

        home_id = await self._api.resolve_home_id(self._home_id)
        if self._api.realtime_enabled(home_id) is False:
            _log.warning(
                "Tibber home %s has no real-time consumption (Pulse/Watty) — live power unavailable",
                home_id,
            )
        url = await self._api.websocket_url()
        got_data = False
        async with connect(
            url,
            subprotocols=["graphql-transport-ws"],  # type: ignore[list-item]
            user_agent_header=_user_agent(),
            additional_headers={"Authorization": self._api.token},
            ssl=_ssl_context(),
            open_timeout=20,
        ) as ws:
            await ws.send(json.dumps({"type": "connection_init", "payload": {"token": self._api.token}}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if ack.get("type") != "connection_ack":
                raise TibberApiError(f"Tibber websocket not acknowledged: {ack}")
            await ws.send(json.dumps({
                "id": "1",
                "type": "subscribe",
                "payload": {"query": _LIVE_SUBSCRIPTION % home_id},
            }))
            _log.info("Tibber live stream connected (home %s)", home_id)

            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), _LIVE_STALL_S))
                kind = msg.get("type")
                if kind == "next":
                    payload = msg.get("payload") or {}
                    if payload.get("errors"):
                        raise TibberApiError(f"Tibber live stream error: {payload['errors']}")
                    m = _parse_live(payload.get("data") or {})
                    if m is not None:
                        self._store(m)
                        got_data = True
                elif kind == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif kind == "error":
                    raise TibberApiError(f"Tibber live stream error: {msg.get('payload')}")
                elif kind == "complete":
                    _log.info("Tibber live stream completed by server")
                    return got_data


# ---------------------------------------------------------------------------
# Shared instances
# ---------------------------------------------------------------------------

_apis: dict[tuple[str, str], TibberApi] = {}


def get_api(token: str, resolution: str = "QUARTER_HOURLY") -> TibberApi:
    key = (token, resolution)
    api = _apis.get(key)
    if api is None:
        api = TibberApi(token, resolution=resolution)
        _apis[key] = api
    return api

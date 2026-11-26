"""Home Assistant client: websocket state cache, REST for the rest.

Entity reads (``get_entity_state``, ``get_entity``, ``list_entities``) and
service calls go over one persistent websocket (``websocket.HAWebSocket``):
the states of all entities are kept in memory and updated as Home Assistant
reports changes, so a control tick reads no entity over the network.  While
the websocket is down they fall back to the REST API.  History, statistics
and service calls that return data stay on REST (large, rare requests).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import datetime
from typing import Any, Protocol, Self, runtime_checkable

import httpx

from .client_types import EntityState, parse_ts
from .websocket import HAWebSocket

__all__ = ["EntityState", "HAClient", "HAClientProtocol"]

_log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
# Long-term statistics queries span months of hourly rows — far slower than
# a state read.
_STATISTICS_TIMEOUT = 120.0
#: How long the first reads wait for the websocket's initial sync before they
#: use REST (startup: the first poll runs while the connection is set up).
_STARTUP_WAIT_S = 5.0

_parse_ts = parse_ts  # kept for callers of the old private name

#: Attributes that list what an entity can be set to (entity selector).
_OPTION_ATTRIBUTES = ("options", "hvac_modes", "preset_modes", "operation_list",
                      "fan_modes", "swing_modes")
#: Attributes not offered for an attribute binding.
_HIDDEN_ATTRIBUTES = frozenset({"friendly_name", "icon", "entity_picture", "supported_features",
                                "attribution", "unit_of_measurement", "device_class",
                                "state_class", "editable", "id", "restored"})


@runtime_checkable
class HAClientProtocol(Protocol):
    """Structural interface for Home Assistant clients (real or fake)."""

    async def get_entity_state(self, entity_id: str) -> Any:
        """Return the state string for *entity_id*."""
        ...

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        """Call a Home Assistant service."""
        ...


class HAClient:
    """Thin async HTTP client for the Home Assistant REST API.

    Authenticates with a long-lived access token.

    Parameters
    ----------
    url:
        Full base URL, e.g. ``"https://ha.example.com"`` or
        ``"http://192.168.1.5:8123"``.
    token:
        Long-lived access token created in the HA profile page.
    timeout:
        Per-request timeout in seconds.
    ws_url:
        Websocket URL, when it is not ``<url>/api/websocket`` (the Supervisor
        proxy serves it at ``ws://supervisor/core/websocket``).
    """

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = _DEFAULT_TIMEOUT,
        ws_url: str | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        self._base_url = url.rstrip("/")
        self._token = token
        self._ws_url_override = ws_url
        self._timeout = timeout
        self._ws: HAWebSocket | None = None
        self._startup_deadline: float | None = None

    # ------------------------------------------------------------------
    # Websocket
    # ------------------------------------------------------------------

    def _ws_url(self) -> str:
        if self._ws_url_override:
            return self._ws_url_override
        return "ws" + self._base_url[len("http"):] + "/api/websocket"

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self._base_url.startswith("https"):
            return None
        # Same CA bundle httpx uses, so a slim image without system CAs
        # still verifies the certificate.
        import certifi

        return ssl.create_default_context(cafile=certifi.where())

    def _websocket(self) -> HAWebSocket:
        """The websocket, started on first use (needs a running loop)."""
        if self._ws is None:
            self._ws = HAWebSocket(
                self._ws_url(), self._token,
                ssl_context=self._ssl_context(), timeout=self._timeout,
            )
            self._startup_deadline = asyncio.get_running_loop().time() + _STARTUP_WAIT_S
        self._ws.start()
        return self._ws

    async def _cache(self) -> HAWebSocket | None:
        """The websocket when its state cache is in sync, else ``None`` (use REST).

        Until the first sync (at most ``_STARTUP_WAIT_S`` after the first
        use) reads wait for it instead of falling back right away.
        """
        ws = self._websocket()
        if ws.ready.is_set():
            return ws
        assert self._startup_deadline is not None
        remaining = self._startup_deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            try:
                await asyncio.wait_for(ws.ready.wait(), remaining)
            except TimeoutError:
                pass
        return ws if ws.ready.is_set() else None

    async def start(self) -> None:
        """Connect the websocket now (optional: the first read does it too)."""
        await self._cache()

    @property
    def cache_ready(self) -> bool:
        """The websocket state cache is in sync (reads need no request)."""
        return self._ws is not None and self._ws.ready.is_set()

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    async def get_entity_state(self, entity_id: str) -> Any:
        """Return the raw state string for *entity_id* (``None`` if HA does not know it)."""
        cache = await self._cache()
        if cache is not None:
            entity = cache.get(entity_id)
            return entity.state if entity is not None else None
        resp = await self._client.get(f"/api/states/{entity_id}")
        resp.raise_for_status()
        return resp.json().get("state")

    async def list_entities(self) -> list[dict[str, str]]:
        """Every entity as ``{entity_id, name}``, sorted by id."""
        cache = await self._cache()
        if cache is not None:
            return sorted(
                (
                    {"entity_id": eid,
                     "name": str(entity.attributes.get("friendly_name") or "")}
                    for eid, entity in cache.entities().items()
                ),
                key=lambda e: e["entity_id"],
            )
        resp = await self._client.get("/api/states")
        resp.raise_for_status()
        return sorted(
            (
                {
                    "entity_id": str(item.get("entity_id")),
                    "name": str((item.get("attributes") or {}).get("friendly_name") or ""),
                }
                for item in resp.json()
                if item.get("entity_id")
            ),
            key=lambda e: e["entity_id"],
        )

    async def entity_catalog(self) -> list[dict[str, Any]]:
        """Every entity with what the config UI's entity selector shows and
        offers, sorted by id: ``entity_id``, ``name``, ``state``, ``unit``,
        ``device_class``, ``area`` (from HA's registries, when they can be
        read), ``options`` (a select's options, a climate's HVAC modes, …, per
        attribute) and ``attributes`` (the attribute names)."""
        cache = await self._cache()
        if cache is not None:
            states = cache.entities()
        else:
            resp = await self._client.get("/api/states")
            resp.raise_for_status()
            states = {str(item["entity_id"]): EntityState.from_api(item)
                      for item in resp.json() if item.get("entity_id")}
        areas = await self._entity_areas()
        out = []
        for eid, entity in states.items():
            attrs = entity.attributes
            options = {key: [str(o) for o in attrs[key]]
                       for key in _OPTION_ATTRIBUTES if isinstance(attrs.get(key), list)}
            out.append({
                "entity_id": eid,
                "name": str(attrs.get("friendly_name") or ""),
                "state": entity.state,
                "unit": attrs.get("unit_of_measurement"),
                "device_class": attrs.get("device_class"),
                "area": areas.get(eid),
                "options": options,
                "attributes": sorted(k for k in attrs if k not in _HIDDEN_ATTRIBUTES),
            })
        return sorted(out, key=lambda e: e["entity_id"])

    async def _entity_areas(self) -> dict[str, str]:
        """entity_id → area name, from the entity, device and area registries
        (websocket; empty when they cannot be read, e.g. a non-admin token)."""
        ws = self._websocket()
        if not ws.connected:
            return {}
        try:
            entities = await ws.command({"type": "config/entity_registry/list_for_display"})
            devices = await ws.command({"type": "config/device_registry/list"})
            area_list = await ws.command({"type": "config/area_registry/list"})
        except Exception as exc:  # noqa: BLE001 — areas are a nice-to-have
            _log.debug("Home Assistant registries not readable: %s", exc)
            return {}
        names = {a["area_id"]: a["name"] for a in area_list or [] if a.get("area_id")}
        device_area = {d["id"]: d.get("area_id") for d in devices or [] if d.get("id")}
        out: dict[str, str] = {}
        for e in (entities or {}).get("entities", []):
            area_id = e.get("ai") or device_area.get(e.get("di"))
            if area_id in names:
                out[e["ei"]] = names[area_id]
        return out

    async def get_entity(self, entity_id: str) -> EntityState | None:
        """Return state, attributes and timestamps of *entity_id* (None if unknown)."""
        cache = await self._cache()
        if cache is not None:
            return cache.get(entity_id)
        resp = await self._client.get(f"/api/states/{entity_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return EntityState.from_api(resp.json())

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        """Call a service: over the websocket when it is connected, else REST.

        Raises when Home Assistant reports an error (or the connection drops
        mid-call); callers retry on their next control tick.
        """
        ws = self._websocket()
        if ws.connected:
            await ws.call_service(domain, service, data)
            return
        resp = await self._client.post(f"/api/services/{domain}/{service}", json=data)
        resp.raise_for_status()

    async def call_service_with_response(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Call a service that returns data and return its ``service_response``."""
        resp = await self._client.post(
            f"/api/services/{domain}/{service}",
            params={"return_response": ""},
            json=data,
            timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        resp.raise_for_status()
        return resp.json().get("service_response") or {}

    async def get_statistics(
        self,
        statistic_ids: list[str],
        start: datetime,
        end: datetime | None = None,
        period: str = "hour",
        types: tuple[str, ...] = ("mean", "min", "max"),
        units: dict[str, str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return long-term statistics from the HA recorder.

        Uses the ``recorder.get_statistics`` action (HA 2025.6+).  Unlike the
        state history, long-term statistics are kept indefinitely.

        Returns ``{statistic_id: [row, …]}`` where each row is a dict with a
        timezone-aware ``start`` datetime plus the requested *types*
        (values may be ``None``).  Ids without statistics are absent.

        *units* asks HA to convert per unit class, e.g.
        ``{"power": "W", "energy": "Wh"}`` (statistics in other classes are
        returned unchanged).
        """
        body: dict[str, Any] = {
            "statistic_ids": list(statistic_ids),
            "start_time": start.isoformat(),
            "period": period,
            "types": list(types),
        }
        if end is not None:
            body["end_time"] = end.isoformat()
        if units:
            body["units"] = dict(units)
        response = await self.call_service_with_response(
            "recorder", "get_statistics", body, timeout=_STATISTICS_TIMEOUT
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for stat_id, rows in (response.get("statistics") or {}).items():
            parsed = []
            for row in rows:
                ts = _parse_ts(row.get("start"))
                if ts is not None:
                    parsed.append({**row, "start": ts})
            out[stat_id] = parsed
        return out

    async def get_history(
        self,
        entity_ids: list[str],
        start: datetime,
        end: datetime | None = None,
    ) -> dict[str, list[tuple[datetime, str]]]:
        """Return the recorded state changes of *entity_ids* in ``[start, end]``.

        Uses ``/api/history/period`` (full resolution, kept as long as the
        recorder keeps states — 10 days by default).  Returns
        ``{entity_id: [(changed_at, state), …]}`` with timezone-aware
        timestamps; the first entry is the state at *start*.
        """
        params: dict[str, Any] = {
            "filter_entity_id": ",".join(entity_ids),
            "minimal_response": "",
            "no_attributes": "",
        }
        if end is not None:
            params["end_time"] = end.isoformat()
        resp = await self._client.get(
            f"/api/history/period/{start.isoformat()}",
            params=params,
            timeout=_STATISTICS_TIMEOUT,
        )
        resp.raise_for_status()
        out: dict[str, list[tuple[datetime, str]]] = {}
        for series in resp.json() or []:
            if not series:
                continue
            entity_id = series[0].get("entity_id")
            rows: list[tuple[datetime, str]] = []
            for item in series:
                ts = _parse_ts(item.get("last_changed") or item.get("last_updated"))
                if ts is not None:
                    rows.append((ts, str(item.get("state"))))
            if entity_id:
                out[entity_id] = rows
        return out

    async def get_weather_forecast(
        self, entity_id: str, forecast_type: str = "hourly"
    ) -> list[tuple[datetime, float]]:
        """Return ``(timestamp, temperature)`` points from a weather entity.

        Uses the ``weather.get_forecasts`` action.  Points without a
        temperature are skipped.
        """
        response = await self.call_service_with_response(
            "weather", "get_forecasts", {"entity_id": entity_id, "type": forecast_type}
        )
        points: list[tuple[datetime, float]] = []
        for item in (response.get(entity_id) or {}).get("forecast") or []:
            ts = _parse_ts(item.get("datetime"))
            temp = item.get("temperature")
            if ts is not None and isinstance(temp, (int, float)):
                points.append((ts, float(temp)))
        return points

    async def get_energy_prefs(self) -> dict[str, Any]:
        """Return the Energy dashboard configuration (``energy/get_prefs``).

        Only available over the WebSocket API: uses the open connection, or a
        short-lived one while it is down.
        """
        ws = self._websocket()
        if ws.connected:
            return await ws.command({"type": "energy/get_prefs"}) or {}

        import json

        import websockets

        token = self._token
        async with websockets.connect(
            self._ws_url(), max_size=None, open_timeout=self._timeout, ssl=self._ssl_context()
        ) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant WebSocket auth failed: {auth.get('type')}")
            await ws.send(json.dumps({"id": 1, "type": "energy/get_prefs"}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    break
        if not msg.get("success"):
            raise RuntimeError(f"energy/get_prefs failed: {msg.get('error')}")
        return msg.get("result") or {}

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

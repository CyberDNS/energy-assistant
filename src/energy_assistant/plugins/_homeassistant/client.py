"""Home Assistant REST API client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import httpx

_DEFAULT_TIMEOUT = 10.0
# Long-term statistics queries span months of hourly rows — far slower than
# a state read.
_STATISTICS_TIMEOUT = 120.0


def _parse_ts(value: Any) -> datetime | None:
    """Parse a timestamp as HA returns it: ISO string or epoch (s or ms)."""
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            return None
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return None


@dataclass(frozen=True)
class EntityState:
    """One entity's state as the REST API returns it."""

    state: str | None
    attributes: dict[str, Any]
    last_changed: datetime | None
    last_updated: datetime | None
    last_reported: datetime | None
    """When the integration last reported a value, even an unchanged one
    (HA 2024.3+).  Falls back to ``last_updated`` on older versions."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> EntityState:
        updated = _parse_ts(data.get("last_updated"))
        return cls(
            state=data.get("state"),
            attributes=dict(data.get("attributes") or {}),
            last_changed=_parse_ts(data.get("last_changed")),
            last_updated=updated,
            last_reported=_parse_ts(data.get("last_reported")) or updated,
        )


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
    """

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def get_entity_state(self, entity_id: str) -> Any:
        """Return the raw state string for *entity_id*, or ``None`` on error."""
        resp = await self._client.get(f"/api/states/{entity_id}")
        resp.raise_for_status()
        return resp.json().get("state")

    async def list_entities(self) -> list[dict[str, str]]:
        """Every entity as ``{entity_id, name}``, sorted by id."""
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

    async def get_entity(self, entity_id: str) -> EntityState | None:
        """Return state, attributes and timestamps of *entity_id* (None if unknown)."""
        resp = await self._client.get(f"/api/states/{entity_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return EntityState.from_api(resp.json())

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        """POST to ``/api/services/{domain}/{service}`` with *data* as JSON body."""
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

        Only available over the WebSocket API; opens a short-lived connection
        with the same token.
        """
        import json

        import websockets

        base = str(self._client.base_url).rstrip("/")
        ws_url = "ws" + base[len("http"):] + "/api/websocket"
        token = self._client.headers["Authorization"].removeprefix("Bearer ")
        ssl_ctx = None
        if ws_url.startswith("wss"):
            # Same CA bundle httpx uses, so a slim image without system CAs
            # still verifies the certificate.
            import ssl

            import certifi

            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        async with websockets.connect(
            ws_url, max_size=None, open_timeout=_DEFAULT_TIMEOUT, ssl=ssl_ctx
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
        await self._client.aclose()

    async def __aenter__(self) -> "HAClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

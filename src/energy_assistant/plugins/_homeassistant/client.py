"""Home Assistant REST API client."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import httpx

_DEFAULT_TIMEOUT = 10.0
# History queries can span many days and thousands of state-change rows —
# slower than a simple current-state read, so they get a longer budget.
_HISTORY_TIMEOUT = 60.0

_log = logging.getLogger(__name__)


@runtime_checkable
class HAClientProtocol(Protocol):
    """Structural interface for Home Assistant clients (real or fake)."""

    async def get_entity_state(self, entity_id: str) -> Any:
        """Return the state string for *entity_id*."""
        ...

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        """Return the full state object (``state`` + ``attributes``) for *entity_id*."""
        ...

    async def get_weather_forecast(
        self, entity_id: str, forecast_type: str = "hourly"
    ) -> list[dict[str, Any]]:
        """Return forecast points (``[{"datetime": ..., "temperature": ...}, ...]``)."""
        ...

    async def get_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        """Return ``(timestamp, raw_state)`` pairs for *entity_id* in ``[start, end]``."""
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

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        """Return the full state object (``state`` + ``attributes``) for *entity_id*."""
        resp = await self._client.get(f"/api/states/{entity_id}")
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def get_weather_forecast(
        self, entity_id: str, forecast_type: str = "hourly"
    ) -> list[dict[str, Any]]:
        """Return forecast points for *entity_id*.

        Tries the modern ``weather.get_forecasts`` service (HA 2023.9+, which
        removed the ``forecast`` attribute from weather entities) first, then
        falls back to reading the ``forecast`` attribute directly for older
        HA versions that still expose it that way.
        """
        try:
            resp = await self._client.post(
                "/api/services/weather/get_forecasts",
                params={"return_response": "true"},
                json={"entity_id": entity_id, "type": forecast_type},
            )
            resp.raise_for_status()
            body = resp.json()
            forecasts = body.get("service_response", body)
            points = forecasts.get(entity_id, {}).get("forecast", [])
            if points:
                return list(points)
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            _log.debug(
                "weather.get_forecasts service call failed for %s (%s), "
                "falling back to forecast attribute",
                entity_id, exc,
            )

        entity = await self.get_entity(entity_id)
        points = entity.get("attributes", {}).get("forecast", [])
        return list(points)

    async def get_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        """Return raw ``(timestamp, state)`` history for *entity_id* in ``[start, end]``.

        Uses the recorder's ``/api/history/period`` REST endpoint — state
        strings are returned as-is (not parsed to float), since callers
        interpret them differently (numeric sensors vs. e.g. person
        "home"/"not_home" states). Only covers whatever the HA recorder
        currently retains (commonly ~10 days by default, configurable via
        HA's ``recorder:`` settings) — long-term statistics beyond that
        window require HA's WebSocket API, which this client doesn't use.
        """
        resp = await self._client.get(
            f"/api/history/period/{start.isoformat()}",
            params={
                "filter_entity_id": entity_id,
                "end_time": end.isoformat(),
                "minimal_response": "true",
                "no_attributes": "true",
            },
            timeout=_HISTORY_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body:
            return []
        points: list[tuple[datetime, str]] = []
        for entry in body[0]:
            state = entry.get("state")
            last_changed = entry.get("last_changed")
            if state is None or last_changed is None:
                continue
            try:
                ts = datetime.fromisoformat(str(last_changed).replace("Z", "+00:00"))
            except ValueError:
                continue
            points.append((ts, state))
        return points

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        """POST to ``/api/services/{domain}/{service}`` with *data* as JSON body."""
        resp = await self._client.post(f"/api/services/{domain}/{service}", json=data)
        resp.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HAClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

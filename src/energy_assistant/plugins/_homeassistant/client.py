"""Home Assistant REST API client."""

from __future__ import annotations

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
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return None


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
    ) -> dict[str, list[dict[str, Any]]]:
        """Return long-term statistics from the HA recorder.

        Uses the ``recorder.get_statistics`` action (HA 2025.6+).  Unlike the
        state history, long-term statistics are kept indefinitely.

        Returns ``{statistic_id: [row, …]}`` where each row is a dict with a
        timezone-aware ``start`` datetime plus the requested *types*
        (values may be ``None``).  Ids without statistics are absent.
        """
        body: dict[str, Any] = {
            "statistic_ids": list(statistic_ids),
            "start_time": start.isoformat(),
            "period": period,
            "types": list(types),
        }
        if end is not None:
            body["end_time"] = end.isoformat()
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

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HAClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

"""
Async HTTP client for the Home Assistant REST API.

Reads entity states from a local Home Assistant instance using a
long-lived access token.

Lifecycle
---------
Use as an async context manager or call ``await client.close()`` explicitly::

    async with HAClient("https://home.example.org", token="ey...") as client:
        state = await client.get_entity_state("sensor.mt175_mt175_p")

    async with HAClient("http://192.168.2.40:8123", token="ey...") as client:
        state = await client.get_entity_state("sensor.mt175_mt175_p")
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx


@runtime_checkable
class HAClientProtocol(Protocol):
    """Structural interface for HA client implementations (real + fake for tests)."""

    async def get_entity_state(self, entity_id: str) -> Any:
        """Return the raw ``state`` string for *entity_id*, or ``None`` if unavailable."""
        ...


class HAClient:
    """
    Async HTTP client for the Home Assistant REST API.

    Parameters
    ----------
    url:
        Base URL of the Home Assistant instance, e.g.
        ``"https://home.example.org"`` or ``"http://192.168.2.40:8123"``.
    token:
        Long-lived access token.
    timeout:
        Request timeout in seconds (default 10).
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout),
        )

    async def get_entity_state(self, entity_id: str) -> Any:
        """
        Return the raw ``state`` string for *entity_id*.

        Returns ``None`` if the entity does not exist or the request fails.
        """
        response = await self._http.get(f"/api/states/{entity_id}")
        if not response.is_success:
            return None
        data = response.json()
        return data.get("state")

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "HAClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

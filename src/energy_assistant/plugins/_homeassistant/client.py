"""Home Assistant REST API client."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

_DEFAULT_TIMEOUT = 10.0


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

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HAClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

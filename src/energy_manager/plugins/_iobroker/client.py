"""
ioBroker simple-api HTTP client.

The simple-api adapter (ioBroker adapter ``simple-api``) exposes device
states as HTTP endpoints.  Enable it in ioBroker and note its port
(default: 8087).

Optional authentication
-----------------------
If you have configured a ``basicAuth`` username/password or a ``Bearer``
token in the simple-api adapter settings, pass ``api_token`` here.  The
token is sent as ``Authorization: Bearer <token>`` on every request.

Lifecycle
---------
The client wraps an ``httpx.AsyncClient`` that is opened at construction
time.  Call ``await client.close()`` (or use it as an async context manager)
when the application shuts down.

    client = IoBrokerClient("192.168.1.5", port=8087, api_token="secret")
    try:
        value = await client.get_value("fronius.0.inverters.0.Power")
    finally:
        await client.close()

Or with async context manager (convenient in tests)::

    async with IoBrokerClient("192.168.1.5") as client:
        value = await client.get_value("fronius.0.inverters.0.Power")

Connection sharing
------------------
If multiple plugins connect to the same ioBroker instance, use
``IoBrokerConnectionPool`` (``energy_manager.plugins._iobroker.pool``) to
obtain a shared ``IoBrokerClient`` instead of constructing one directly.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

import httpx


# ---------------------------------------------------------------------------
# Client protocol — use this for type annotations so tests can inject fakes
# ---------------------------------------------------------------------------


@runtime_checkable
class IoBrokerClientProtocol(Protocol):
    """Structural interface for ioBroker client implementations."""

    async def get_value(self, object_id: str) -> Any:
        """Return the current ``val`` of an ioBroker state object."""
        ...

    async def get_bulk(self, object_ids: list[str]) -> dict[str, Any]:
        """Return a mapping of object_id → current value for all *object_ids*."""
        ...

    async def set_value(self, object_id: str, value: Any) -> None:
        """Write *value* to an ioBroker state object."""
        ...


# ---------------------------------------------------------------------------
# Real HTTP client
# ---------------------------------------------------------------------------


class IoBrokerClient:
    """
    Async HTTP client for the ioBroker simple-api adapter.

    Parameters
    ----------
    host:
        Hostname or IP address of the ioBroker instance.
    port:
        Port of the simple-api adapter (default 8087).
    api_token:
        Optional bearer token.  Leave ``None`` if the adapter requires no auth.
    timeout:
        Request timeout in seconds (default 10).
    """

    def __init__(
        self,
        host: str,
        port: int = 8087,
        api_token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        headers: dict[str, str] = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._http = httpx.AsyncClient(
            base_url=f"http://{host}:{port}",
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    async def __aenter__(self) -> "IoBrokerClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def get_value(self, object_id: str) -> Any:
        """
        Return the current value of an ioBroker state.

        Calls ``GET /get/<object_id>`` and returns the ``val`` field.
        Returns ``None`` if the object does not exist or has no value.
        """
        response = await self._http.get(f"/get/{object_id}")
        response.raise_for_status()
        data = response.json()
        return data.get("val")

    async def get_bulk(self, object_ids: list[str]) -> dict[str, Any]:
        """
        Return the current values for all *object_ids* concurrently.

        Uses ``asyncio.gather`` to issue requests in parallel.
        """
        if not object_ids:
            return {}
        values = await asyncio.gather(*(self.get_value(oid) for oid in object_ids))
        return dict(zip(object_ids, values))

    async def set_value(self, object_id: str, value: Any) -> None:
        """Write *value* to an ioBroker state via ``GET /set/<id>?value=``."""
        response = await self._http.get(f"/set/{object_id}", params={"value": str(value)})
        response.raise_for_status()

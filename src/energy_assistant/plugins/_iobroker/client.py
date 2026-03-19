"""ioBroker simple-api HTTP client."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

_DEFAULT_TIMEOUT = 5.0


@runtime_checkable
class IoBrokerClientProtocol(Protocol):
    """Structural interface for ioBroker clients (real or fake)."""

    async def get_value(self, oid: str) -> Any:
        """Read a single OID value."""
        ...

    async def get_bulk(self, oids: list[str]) -> dict[str, Any]:
        """Read multiple OIDs in one request; return mapping OID → value."""
        ...

    async def set_value(self, oid: str, value: Any) -> None:
        """Write *value* to *oid*."""
        ...


class IoBrokerClient:
    """Thin async HTTP client for the ioBroker simple-api.

    All HTTP requests use ``GET`` endpoints provided by the ioBroker
    simple-api adapter (typically running on port 8087).

    Parameters
    ----------
    host:
        ioBroker host, e.g. ``"192.168.1.5"``.
    port:
        simple-api port (default 8087).
    api_token:
        Optional Bearer token, set in the simple-api adapter security settings.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        host: str,
        port: int = 8087,
        api_token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        headers: dict[str, str] = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = httpx.AsyncClient(
            base_url=f"http://{host}:{port}",
            headers=headers,
            timeout=timeout,
        )

    async def get_value(self, oid: str) -> Any:
        """Read a single OID and return its ``val`` field."""
        resp = await self._client.get("/get", params={"id": oid, "prettyPrint": "false"})
        resp.raise_for_status()
        data = resp.json()
        return data.get("val") if isinstance(data, dict) else None

    async def get_bulk(self, oids: list[str]) -> dict[str, Any]:
        """Read multiple OIDs in one request; return a mapping OID → value."""
        if not oids:
            return {}
        params = [("id[]", oid) for oid in oids]
        resp = await self._client.get("/getBulk", params=params)
        resp.raise_for_status()
        items = resp.json()
        return {item["id"]: item.get("val") for item in items if "id" in item}

    async def set_value(self, oid: str, value: Any) -> None:
        """Write *value* to *oid*."""
        resp = await self._client.get("/set", params={"id": oid, "value": str(value)})
        resp.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "IoBrokerClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

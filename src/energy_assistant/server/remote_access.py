"""Token-protected, read-only access to the API from outside Home Assistant.

Inside the Home Assistant add-on the API is normally reached only through
ingress, so it carries no authentication of its own.  To let tools outside HA
(scripts, a debugging assistant via MCP) read it, the add-on can publish its
port; this middleware then guards every request that does *not* come through
ingress:

* no ``server.api_token`` configured → nothing changes, every request passes
  (local development, and installs that never publish the port);
* the request comes from a trusted address (HA ingress proxy, localhost) →
  passes, so the web UI behind ingress keeps full control;
* anyone else must send ``Authorization: Bearer <api_token>`` and only gets
  read access: ``GET``/``HEAD`` on the REST API plus the MCP endpoint, whose
  tools are all read-only.  Writes (``POST``/``PUT``/``DELETE`` on ``/api``)
  are refused with 403.

``/health`` is always open (container health checks).
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Awaitable, Callable, Iterable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Home Assistant's Supervisor proxies all ingress traffic from this address.
HA_INGRESS_PROXY = "172.30.32.2"
DEFAULT_TRUSTED_HOSTS = (HA_INGRESS_PROXY, "127.0.0.1", "::1")

MCP_PATH = "/mcp"
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_OPEN_PATHS = frozenset({"/health"})


class ApiAccessMiddleware:
    """Pure ASGI middleware (works with streaming responses such as SSE)."""

    def __init__(
        self,
        app: ASGIApp,
        token: str | None,
        trusted_hosts: Iterable[str] = DEFAULT_TRUSTED_HOSTS,
    ) -> None:
        self._app = app
        self._token = (token or "").strip().encode() or None
        self._trusted = frozenset(trusted_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self._token is None:
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        client = scope.get("client")
        if path in _OPEN_PATHS or (client is not None and client[0] in self._trusted):
            await self._app(scope, receive, send)
            return

        if scope["type"] == "websocket":  # the API has none; never expose one
            await send({"type": "websocket.close", "code": 1008})
            return

        if not self._has_token(scope):
            await _deny(send, 401, "missing or invalid bearer token",
                        {"www-authenticate": "Bearer"})
            return

        method = scope.get("method", "GET").upper()
        is_mcp = path == MCP_PATH or path.startswith(MCP_PATH + "/")
        if not is_mcp and method not in _READ_METHODS:
            await _deny(send, 403, "the API token grants read-only access")
            return
        await self._app(scope, receive, send)

    def _has_token(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, credential = value.partition(b" ")
                return scheme.lower() == b"bearer" and hmac.compare_digest(
                    credential.strip(), self._token or b""
                )
        return False


async def _deny(send: Send, status: int, detail: str, extra: dict[str, str] | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})

"""A minimal Home Assistant websocket server for client tests.

Speaks the auth handshake, ``subscribe_entities`` (compressed states, as
Home Assistant sends them), ``call_service`` and ``energy/get_prefs``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed


class FakeHAWebSocketServer:
    def __init__(self, states: dict[str, dict[str, Any]] | None = None,
                 token: str = "token") -> None:
        #: entity_id -> compressed state {"s", "a", "lc", ["lu"]}
        self.states: dict[str, dict[str, Any]] = dict(states or {})
        self.token = token
        self.service_calls: list[dict[str, Any]] = []
        self.service_error: dict[str, str] | None = None
        self.energy_prefs: dict[str, Any] = {}
        self.connections = 0
        self._subscribers: list[tuple[ServerConnection, int]] = []
        self._server: Any = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> Self:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def push(self, event: dict[str, Any]) -> None:
        """Send a ``subscribe_entities`` event to every subscriber."""
        for ws, sub_id in list(self._subscribers):
            await ws.send(json.dumps({"id": sub_id, "type": "event", "event": event}))

    async def drop_connections(self) -> None:
        for ws, _ in list(self._subscribers):
            await ws.close()
        self._subscribers.clear()

    async def _handle(self, ws: ServerConnection) -> None:
        self.connections += 1
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.11.2"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != self.token:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.11.2"}))
        try:
            async for raw in ws:
                await self._command(ws, json.loads(raw))
        except ConnectionClosed:
            return
        finally:
            self._subscribers = [s for s in self._subscribers if s[0] is not ws]

    async def _command(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        msg_id, kind = msg["id"], msg["type"]
        if kind == "subscribe_entities":
            await ws.send(json.dumps({"id": msg_id, "type": "result", "success": True, "result": None}))
            self._subscribers.append((ws, msg_id))
            await ws.send(json.dumps({"id": msg_id, "type": "event",
                                      "event": {"a": self.states}}))
        elif kind == "call_service":
            self.service_calls.append(msg)
            if self.service_error is not None:
                await ws.send(json.dumps({"id": msg_id, "type": "result", "success": False,
                                          "error": self.service_error}))
            else:
                await ws.send(json.dumps({"id": msg_id, "type": "result", "success": True,
                                          "result": {"context": {"id": "x"}}}))
        elif kind == "energy/get_prefs":
            await ws.send(json.dumps({"id": msg_id, "type": "result", "success": True,
                                      "result": self.energy_prefs}))
        else:
            await ws.send(json.dumps({"id": msg_id, "type": "result", "success": False,
                                      "error": {"code": "unknown_command", "message": kind}}))


async def until(predicate: Any, timeout: float = 2.0) -> None:
    """Wait until *predicate()* is true."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)

"""Persistent Home Assistant websocket connection with an entity state cache.

One connection per ``HAClient``.  It subscribes to every entity once
(``subscribe_entities``): Home Assistant sends the full state of all
entities first and then only the changes, so device reads come from memory
instead of a REST call per entity and control tick.  Service calls and other
commands use the same connection.

The connection reconnects by itself (with backoff) and resynchronises the
whole cache on every reconnect.  ``ready`` is set while the cache is in sync;
``HAClient`` falls back to REST while it is not.

Compressed states
-----------------
``subscribe_entities`` sends states as ``{"s": state, "a": attributes,
"c": context, "lc": last_changed, "lu": last_updated}`` (epoch seconds;
``lu`` is absent when it equals ``lc``).  Events:

``{"a": {entity_id: state}}``                 entities added (the first event: all)
``{"c": {entity_id: {"+": {…}, "-": {"a": [keys]}}}}``  changes; ``+`` attributes are merged
``{"r": [entity_id, …]}``                     entities removed

The websocket does not report ``last_reported`` (an integration reporting an
unchanged value): ``EntityState.last_reported`` is ``last_updated`` here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any

from .client_types import EntityState, parse_ts

_log = logging.getLogger(__name__)

#: Seconds between reconnect attempts (the last value repeats).
_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
#: Wait after a rejected token: retrying fast does not help.
_AUTH_FAILED_BACKOFF_S = 60.0


class HAWebSocketError(RuntimeError):
    """A websocket command failed (connection lost or ``success: false``)."""


class HAAuthError(HAWebSocketError):
    """Home Assistant rejected the access token."""


def _entity_state(raw: dict[str, Any]) -> EntityState:
    changed = parse_ts(raw.get("lc"))
    updated = parse_ts(raw.get("lu")) or changed
    return EntityState(
        state=None if raw.get("s") is None else str(raw.get("s")),
        attributes=dict(raw.get("a") or {}),
        last_changed=changed,
        last_updated=updated,
        last_reported=updated,
    )


class HAWebSocket:
    """Keeps a websocket to Home Assistant open and the state cache current.

    Parameters
    ----------
    url:
        Websocket URL, e.g. ``"ws://homeassistant:8123/api/websocket"``.
    token:
        Long-lived access token.
    ssl_context:
        For ``wss://`` URLs.
    timeout:
        Seconds for connecting, authenticating and each command.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._token = token
        self._ssl = ssl_context
        self._timeout = timeout
        self._states: dict[str, dict[str, Any]] = {}
        self._ws: Any = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscription_id: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.ready = asyncio.Event()
        """Set while the state cache is in sync with Home Assistant."""
        self.ha_version: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the connection task (needs a running event loop)."""
        if self._task is None and not self._closed:
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="ha-websocket"
            )

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._ws is not None:
            await self._ws.close()
        self._disconnected()

    @property
    def connected(self) -> bool:
        """Authenticated connection open (commands can be sent)."""
        return self._ws is not None

    # ------------------------------------------------------------------
    # State cache
    # ------------------------------------------------------------------

    def get(self, entity_id: str) -> EntityState | None:
        """Cached state of *entity_id*, ``None`` when HA does not know it."""
        raw = self._states.get(entity_id)
        return _entity_state(raw) if raw is not None else None

    def entities(self) -> dict[str, EntityState]:
        """Every cached entity."""
        return {eid: _entity_state(raw) for eid, raw in self._states.items()}

    def _apply(self, event: dict[str, Any], *, initial: bool) -> None:
        added = event.get("a") or {}
        if initial:
            # First event of a (re)subscription: the complete set.
            self._states = {eid: dict(raw) for eid, raw in added.items()}
        else:
            for eid, raw in added.items():
                self._states[eid] = dict(raw)
        for eid, diff in (event.get("c") or {}).items():
            current = self._states.get(eid)
            if current is None:
                continue  # a change before the entity's add: wait for the next resync
            plus = diff.get("+") or {}
            for key, value in plus.items():
                if key == "a":
                    current["a"] = {**(current.get("a") or {}), **value}
                else:
                    current[key] = value
            if "lc" in plus and "lu" not in plus:
                current.pop("lu", None)  # last_updated == the new last_changed
            for key in (diff.get("-") or {}).get("a") or ():
                (current.get("a") or {}).pop(key, None)
        for eid in event.get("r") or ():
            self._states.pop(eid, None)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def command(self, message: dict[str, Any], timeout: float | None = None) -> Any:
        """Send a command and return its ``result`` (raises ``HAWebSocketError``)."""
        ws = self._ws
        if ws is None:
            raise HAWebSocketError("Home Assistant websocket is not connected")
        msg_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await ws.send(json.dumps({**message, "id": msg_id}))
            return await asyncio.wait_for(future, timeout or self._timeout)
        except TimeoutError as exc:
            raise HAWebSocketError(
                f"Home Assistant did not answer {message.get('type')!r} in time"
            ) from exc
        finally:
            self._pending.pop(msg_id, None)

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        return_response: bool = False,
        timeout: float | None = None,
    ) -> Any:
        message: dict[str, Any] = {
            "type": "call_service", "domain": domain, "service": service,
            "service_data": data,
        }
        if return_response:
            message["return_response"] = True
        return await self.command(message, timeout)

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        was_connected = False
        while not self._closed:
            try:
                await self._connect_and_listen()
                attempt = 0  # the connection ended normally (server closed it)
                delay = _BACKOFF_S[0]
            except asyncio.CancelledError:
                raise
            except HAAuthError as exc:
                _log.error("Home Assistant websocket: %s — retrying in %.0f s",
                           exc, _AUTH_FAILED_BACKOFF_S)
                delay = _AUTH_FAILED_BACKOFF_S
            except Exception as exc:  # noqa: BLE001 — any failure: reconnect
                delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
                attempt += 1
                log = _log.warning if attempt == 1 or was_connected else _log.debug
                log("Home Assistant websocket %s: %s — reconnecting in %.0f s "
                    "(REST until then)",
                    "lost" if was_connected else "unavailable", exc or type(exc).__name__,
                    delay)
            finally:
                was_connected = self.ready.is_set()
                self._disconnected()
            if not self._closed:
                await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> None:
        import websockets

        async with websockets.connect(
            self._url, max_size=None, open_timeout=self._timeout, ssl=self._ssl,
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), self._timeout))
            self.ha_version = hello.get("ha_version")
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth = json.loads(await asyncio.wait_for(ws.recv(), self._timeout))
            if auth.get("type") != "auth_ok":
                raise HAAuthError(f"authentication failed ({auth.get('type')}: "
                                  f"{auth.get('message', '')})")
            self._ws = ws
            sub_id = self._next_id
            self._next_id += 1
            self._subscription_id = sub_id
            await ws.send(json.dumps({"id": sub_id, "type": "subscribe_entities"}))
            initial = True
            async for raw in ws:
                msg = json.loads(raw)
                if isinstance(msg, list):  # coalesced messages
                    messages = msg
                else:
                    messages = [msg]
                for item in messages:
                    if item.get("id") == sub_id:
                        if item.get("type") == "event":
                            self._apply(item.get("event") or {}, initial=initial)
                            if initial:
                                initial = False
                                self.ready.set()
                                _log.info("Home Assistant websocket connected (HA %s, "
                                          "%d entities)", self.ha_version, len(self._states))
                        elif item.get("type") == "result" and not item.get("success"):
                            raise HAWebSocketError(
                                f"subscribe_entities failed: {item.get('error')}")
                        continue
                    self._resolve(item)

    def _resolve(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "result":
            return
        future = self._pending.get(msg.get("id"))  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if msg.get("success"):
            future.set_result(msg.get("result"))
        else:
            error = msg.get("error") or {}
            future.set_exception(HAWebSocketError(
                f"{error.get('code', 'error')}: {error.get('message', '')}"))

    def _disconnected(self) -> None:
        self._ws = None
        self._subscription_id = None
        self.ready.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HAWebSocketError("Home Assistant websocket closed"))
        self._pending.clear()

"""Poll a Tibber Pulse Bridge on the LAN for raw meter telegrams.

Endpoints (HTTP basic auth, user ``admin``, password printed on the bridge):

``GET /node_data.json?node_id=<n>``
    Raw meter telegram of node *n* (current firmware; despite the name it is
    not JSON).
``GET /data.json?node_id=<n>``
    Same, on older firmware.  Used when the first one answers 404.
``GET /nodes.json``
    Paired nodes with ``available`` and ``last_data_ms``.  Tells whether the IR
    head still receives data from the meter even though the bridge answers.
``GET /node_metrics.json?node_id=<n>``
    Node health (battery voltage, invalid-reading counters).  Diagnostics only.

The poller runs as a background task, started on first use.  Every sample is
stamped with the UTC receive time.  Bad telegrams are skipped and the last
good reading is kept; the reading only becomes unavailable once nothing valid
arrived for ``stale_after_s`` or the bridge reports that the node stopped
delivering data.  When the bridge is unreachable the poll interval backs off
exponentially.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .decode import MeterReading, TelegramError, decode_telegram

_log = logging.getLogger(__name__)
_monotonic = time.monotonic  # module-level so tests can patch the clock

_DATA_PATHS = ("/node_data.json", "/data.json")  # current firmware first
_HTTP_TIMEOUT_S = 5.0
_BACKOFF_MAX_S = 60.0
_NOT_FOUND_RETRY_S = 300.0     # endpoint missing: firmware change, wrong node_id
_NODE_INFO_INTERVAL_S = 30.0   # nodes.json / node_metrics.json refresh
_NODE_INFO_MAX_AGE_S = 120.0   # ignore node info older than this
_INVALID_WARN_STREAK = 10      # warn once after this many bad telegrams in a row


class BridgeError(Exception):
    """Bridge did not deliver a telegram (HTTP error, auth, missing endpoint)."""


class _NotFound(BridgeError):
    pass


@dataclass(frozen=True)
class NodeInfo:
    """Health of the Pulse IR node as reported by the bridge."""

    fetched_mono: float
    available: bool | None = None
    last_data_ms: int | None = None
    battery_voltage: float | None = None
    rssi: float | None = None


class PulseBridgePoller:
    """Background poller for one node of a Tibber Pulse Bridge."""

    def __init__(
        self,
        host: str,
        password: str,
        *,
        user: str = "admin",
        node_id: int = 1,
        poll_interval_s: float = 1.0,
        stale_after_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = host if "://" in host else f"http://{host}"
        self._base_url = base.rstrip("/")
        self._auth = httpx.BasicAuth(user, password)
        self._node_id = node_id
        self._poll_interval_s = max(0.5, float(poll_interval_s))
        self._stale_after_s = max(self._poll_interval_s * 2, float(stale_after_s))
        self._transport = transport
        self._task: asyncio.Task[None] | None = None

        self._data_path: str | None = None
        self._last_payload: bytes | None = None
        self._latest: MeterReading | None = None
        self._latest_mono = 0.0
        self._node: NodeInfo | None = None
        self._next_node_info_mono = 0.0

        self.invalid_streak = 0
        self.invalid_total = 0
        self.error_streak = 0

    @property
    def label(self) -> str:
        return f"{self._base_url} node {self._node_id}"

    # ------------------------------------------------------------------
    # lifecycle

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"tibber_local:{self.label}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # ------------------------------------------------------------------
    # readings

    def latest(self) -> MeterReading | None:
        """The last valid reading, or None when it is stale or the node lost the meter."""
        if self._latest is None:
            return None
        if _monotonic() - self._latest_mono > self._stale_after_s:
            return None
        if self._node_lost_meter():
            return None
        return self._latest

    @property
    def node(self) -> NodeInfo | None:
        if self._node is None or _monotonic() - self._node.fetched_mono > _NODE_INFO_MAX_AGE_S:
            return None
        return self._node

    def _node_lost_meter(self) -> bool:
        node = self.node
        if node is None:
            return False
        if node.available is False:
            return True
        return node.last_data_ms is not None and node.last_data_ms > self._stale_after_s * 1000

    # ------------------------------------------------------------------
    # polling

    async def _run(self) -> None:
        async with httpx.AsyncClient(
            auth=self._auth, timeout=_HTTP_TIMEOUT_S, transport=self._transport
        ) as client:
            while True:
                delay = await self.poll_once(client)
                await asyncio.sleep(delay)

    async def poll_once(self, client: httpx.AsyncClient) -> float:
        """Fetch and decode one telegram; return the delay until the next poll."""
        if _monotonic() >= self._next_node_info_mono:
            self._next_node_info_mono = _monotonic() + _NODE_INFO_INTERVAL_S
            await self._refresh_node_info(client)

        try:
            payload = await self._fetch_telegram(client)
        except _NotFound as exc:
            self._record_error(exc, level=logging.ERROR)
            return _NOT_FOUND_RETRY_S
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — network errors of any kind
            self._record_error(exc)
            return min(self._poll_interval_s * 2 ** self.error_streak, _BACKOFF_MAX_S)

        if self.error_streak:
            _log.info("Tibber Pulse bridge %s reachable again", self.label)
        self.error_streak = 0
        self._handle_payload(payload)
        return self._poll_interval_s

    def _handle_payload(self, payload: bytes, received_at: datetime | None = None) -> None:
        if payload == self._last_payload:
            # The bridge serves the same telegram until the meter sends the next
            # one (every 2-3 s); it carries no news, so freshness is not renewed.
            return
        try:
            reading = decode_telegram(payload, received_at=received_at or datetime.now(timezone.utc))
        except TelegramError as exc:
            self.invalid_streak += 1
            self.invalid_total += 1
            if self.invalid_streak == _INVALID_WARN_STREAK:
                _log.warning(
                    "Tibber Pulse bridge %s: %d undecodable telegrams in a row (last: %s)",
                    self.label, self.invalid_streak, exc,
                )
            else:
                _log.debug("Tibber Pulse bridge %s: skipping telegram: %s", self.label, exc)
            return
        if self.invalid_streak >= _INVALID_WARN_STREAK:
            _log.info("Tibber Pulse bridge %s: valid telegrams again", self.label)
        self.invalid_streak = 0
        self._last_payload = payload
        self._latest = reading
        self._latest_mono = _monotonic()

    async def _fetch_telegram(self, client: httpx.AsyncClient) -> bytes:
        paths = (self._data_path,) if self._data_path else _DATA_PATHS
        for path in paths:
            resp = await client.get(f"{self._base_url}{path}", params={"node_id": self._node_id})
            if resp.status_code == 404:
                continue
            if resp.status_code == 401:
                raise BridgeError("HTTP 401 — wrong bridge password (user 'admin', code on the bridge)")
            resp.raise_for_status()
            if self._data_path != path:
                _log.info("Tibber Pulse bridge %s: reading telegrams from %s", self.label, path)
                self._data_path = path
            return resp.content
        if self._data_path:
            # The known-good endpoint vanished (firmware update?) — probe both again.
            self._data_path = None
            return await self._fetch_telegram(client)
        raise _NotFound(
            f"HTTP 404 on {' and '.join(_DATA_PATHS)} — wrong node_id, or the bridge "
            "firmware no longer exposes raw meter data locally"
        )

    def _record_error(self, exc: Exception, level: int = logging.WARNING) -> None:
        self.error_streak += 1
        # Log the first failure and then only every 10th, so an outage doesn't flood the log.
        if self.error_streak == 1 or self.error_streak % 10 == 0:
            _log.log(
                level, "Tibber Pulse bridge %s: no telegram (%d in a row): %s",
                self.label, self.error_streak, exc,
            )

    async def _refresh_node_info(self, client: httpx.AsyncClient) -> None:
        """Update node availability and health; failures here are not fatal."""
        available: bool | None = None
        last_data_ms: int | None = None
        rssi: float | None = None
        battery: float | None = None
        try:
            resp = await client.get(f"{self._base_url}/nodes.json")
            if resp.status_code == 200:
                for node in resp.json():
                    if int(node.get("node_id", -1)) == self._node_id:
                        available = _as_bool(node.get("available"))
                        last_data_ms = _as_int(node.get("last_data_ms"))
                        rssi = _as_float(node.get("rssi"))
                        break
            resp = await client.get(
                f"{self._base_url}/node_metrics.json", params={"node_id": self._node_id}
            )
            if resp.status_code == 200:
                battery = _as_float((resp.json().get("node") or {}).get("battery_voltage"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.debug("Tibber Pulse bridge %s: node info unavailable: %r", self.label, exc)
        if available is None and last_data_ms is None and battery is None:
            return
        was_lost = self._node_lost_meter()
        self._node = NodeInfo(
            fetched_mono=_monotonic(),
            available=available,
            last_data_ms=last_data_ms,
            battery_voltage=battery,
            rssi=rssi,
        )
        lost = self._node_lost_meter()
        if lost and not was_lost:
            _log.warning(
                "Tibber Pulse bridge %s: node reports no meter data (available=%s, last_data_ms=%s)"
                " — check the IR reader on the meter",
                self.label, available, last_data_ms,
            )


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    f = _as_float(v)
    return int(f) if f is not None else None


def _as_bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None

"""ZendureBridge — one MQTT connection carrying one or more Zendure devices.

Topic layout (identical on the Zendure cloud broker and on a local broker the
device has been redirected to):

``/{productKey}/{deviceKey}/properties/report``
    Device → client.  JSON ``{"properties": {...}, "packData": [...]}``.
    Reports are *deltas*: only changed properties are included, so values are
    merged into a per-device cache.
``iot/{productKey}/{deviceKey}/properties/write``
    Client → device.  JSON ``{"properties": {"<name>": <int>, ...}}``.
``iot/{productKey}/{deviceKey}/properties/read``
    Client → device.  ``{"properties": ["getAll"]}`` asks for a full report.

Both ``/{pk}/{dk}/#`` and ``iot/{pk}/{dk}/#`` are subscribed (replies can come
back on either).  Our own outgoing ``write`` / ``read`` / ``function/invoke``
messages echo back on the ``iot/`` tree and are ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiomqtt

_log = logging.getLogger(__name__)

_RECONNECT_DELAY_S = 30.0
_OUTGOING_SUFFIXES = ("/properties/write", "/properties/read", "/function/invoke")


@dataclass(frozen=True)
class BrokerParams:
    """Everything needed to open the MQTT connection."""

    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    mqtt_v5: bool = False


@dataclass
class DeviceCache:
    """Merged report state of one device."""

    # name → (value, monotonic timestamp of the report that carried it)
    properties: dict[str, tuple[Any, float]] = field(default_factory=dict)
    # pack serial → pack dict (latest report)
    packs: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_message: float | None = None


Key = tuple[str, str]  # (product_key, device_key)


class ZendureBridge:
    """Maintains the MQTT connection and caches device reports.

    *params_factory* is awaited before every connection attempt, so cloud
    credentials are re-fetched on each reconnect (they can rotate).
    Started lazily from ``ensure_started()`` — device factories are sync.
    """

    def __init__(
        self,
        name: str,
        params_factory: Callable[[], Awaitable[BrokerParams]],
        *,
        poll_interval_s: float = 60.0,
    ) -> None:
        self._name = name
        self._params_factory = params_factory
        self._poll_interval_s = poll_interval_s
        self._devices: dict[Key, DeviceCache] = {}
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client is not None

    def register(self, product_key: str, device_key: str) -> None:
        """Track a device.  Register before the first ``ensure_started()``."""
        self._devices.setdefault((product_key, device_key), DeviceCache())

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"zendure_bridge_{self._name}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def cache(self, product_key: str, device_key: str) -> DeviceCache:
        return self._devices.setdefault((product_key, device_key), DeviceCache())

    async def write_properties(
        self, product_key: str, device_key: str, properties: dict[str, int]
    ) -> None:
        """Publish a property write.  Raises when not connected."""
        await self._publish(
            f"iot/{product_key}/{device_key}/properties/write",
            {"properties": properties},
        )

    async def request_full_report(self, product_key: str, device_key: str) -> None:
        await self._publish(
            f"iot/{product_key}/{device_key}/properties/read",
            {"properties": ["getAll"]},
        )

    # ------------------------------------------------------------------
    # Message handling (pure — unit-tested directly)
    # ------------------------------------------------------------------

    def handle_message(self, topic: str, payload: bytes | str, now: float | None = None) -> None:
        if topic.endswith(_OUTGOING_SUFFIXES):
            return
        parts = topic.lstrip("/").split("/")
        if len(parts) < 3:
            return
        if parts[0] == "iot":
            key = (parts[1], parts[2])
        else:
            key = (parts[0], parts[1])
        cache = self._devices.get(key)
        if cache is None:
            return
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            _log.debug("Zendure %s: non-JSON payload on %s", self._name, topic)
            return
        if not isinstance(obj, dict):
            return

        ts = time.monotonic() if now is None else now
        props = obj.get("properties")
        if isinstance(props, dict):
            for name, value in props.items():
                cache.properties[name] = (value, ts)
            # The two directional pack powers are mutually exclusive; a report
            # carrying one non-zero implies the other dropped to 0.
            if _num(props.get("outputPackPower")):
                cache.properties["packInputPower"] = (0, ts)
            if _num(props.get("packInputPower")):
                cache.properties["outputPackPower"] = (0, ts)
        packs = obj.get("packData")
        if isinstance(packs, list):
            for pack in packs:
                if isinstance(pack, dict) and pack.get("sn"):
                    cache.packs[str(pack["sn"])] = {**cache.packs.get(str(pack["sn"]), {}), **pack}
        if isinstance(props, dict) or isinstance(packs, list):
            cache.last_message = ts

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _publish(self, topic: str, body: dict[str, Any]) -> None:
        client = self._client
        if client is None:
            raise ConnectionError(f"Zendure MQTT bridge {self._name!r} not connected")
        await client.publish(topic, json.dumps(body, separators=(",", ":")), qos=1)

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._client = None
                _log.warning(
                    "Zendure MQTT bridge %r disconnected: %s — reconnecting in %.0fs",
                    self._name, exc, _RECONNECT_DELAY_S,
                )
                await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _connect_and_listen(self) -> None:
        p = await self._params_factory()
        kwargs: dict[str, Any] = {"hostname": p.host, "port": p.port}
        if p.username:
            kwargs["username"] = p.username
        if p.password:
            kwargs["password"] = p.password
        if p.client_id:
            kwargs["identifier"] = p.client_id
        if p.mqtt_v5:
            kwargs["protocol"] = aiomqtt.ProtocolVersion.V5

        async with aiomqtt.Client(**kwargs) as client:
            for pk, dk in self._devices:
                await client.subscribe(f"/{pk}/{dk}/#")
                await client.subscribe(f"iot/{pk}/{dk}/#")
            self._client = client
            _log.info(
                "Zendure MQTT bridge %r connected to %s:%d (%d device(s))",
                self._name, p.host, p.port, len(self._devices),
            )
            poller = asyncio.create_task(self._poll_loop())
            try:
                async for message in client.messages:
                    self.handle_message(str(message.topic), message.payload)  # type: ignore[arg-type]
            finally:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
                self._client = None

    async def _poll_loop(self) -> None:
        """Ask every device for a full report now and every poll interval.

        Reports are change-driven; the periodic ``getAll`` refreshes values
        that stopped changing (e.g. a power that went to 0 without a report)
        and keeps the staleness check meaningful.
        """
        while True:
            for pk, dk in list(self._devices):
                try:
                    await self.request_full_report(pk, dk)
                except Exception as exc:  # noqa: BLE001
                    _log.debug("Zendure %s: getAll for %s/%s failed: %s", self._name, pk, dk, exc)
            await asyncio.sleep(self._poll_interval_s)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

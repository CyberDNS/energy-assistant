"""OpenWBMqttDevice — controls an openWB chargepoint directly via MQTT simpleAPI.

Talks to the openWB broker (or a bridged central broker) without going
through Home Assistant.  Topic layout (root topic configurable, e.g.
``openwbmaster``):

Read (subscribed, cached by the shared bridge; verified against a live
openWB 2.x installation — the wiki docs differ in places):
  {root}/simpleAPI/chargepoint/{id}/power       → charging power (W)
  {root}/simpleAPI/chargepoint/{id}/soc/soc     → vehicle SoC (%) — nested,
                                                  NOT a scalar 'soc' topic
  {root}/simpleAPI/chargepoint/{id}/plug_state  → cable plugged ("True"/"False")
  {root}/simpleAPI/chargepoint/{id}/chargemode  → long names: instant_charging|
                                                  pv_charging|stop|…
  {root}/simpleAPI/chargepoint/{id}/charge_state→ charging active (bool)

Write (published on command):
  {root}/simpleAPI/set/chargepoint/{id}/chargemode  → instant|pv|stop
  {root}/set/chargepoint/{id}/set/charge_template   → full template JSON with
      chargemode.instant_charging.{current, limit.selected, limit.soc} updated

The ``set_power_w`` sentinel encoding is identical to the HA-based plugin
(see ``assets/ev.py``):  >500 W → instant, 0<x≤500 W → pv, 0 → stop.

Chargemode set payloads use the SHORT mode names (``instant``/``pv``/``stop``)
while the read topic reports the long ones (``instant_charging``/
``pv_charging``/…) — verified live.  ``_normalize_mode`` maps long → short so
reconciliation compares correctly.

Why the charging current and SoC limit go through a full template write
(all verified live on openWB 2.2.x with two chargepoints):

- The simpleAPI per-id set topics for ``chargecurrent`` /
  ``instant_charging_limit(_soc)`` IGNORE the chargepoint id in the topic
  and always write to the lowest-id chargepoint.  Only ``chargemode`` is
  routed per-id correctly.
- Two rapid simpleAPI sets race inside openWB's read-modify-write of the
  template and one silently reverts the other.
- A single write of the full template JSON to
  ``{root}/set/chargepoint/{id}/set/charge_template`` is atomic, correctly
  per-chargepoint, and is exactly what the openWB UI publishes — so the
  change is also visible in the UI.

The template is the chargepoint's *temporary* charge template — the one
openWB regulates on while ``general/temporary_charge_templates_active`` is
true; the stored vehicle charge profile is not modified.  openWB resets it
from the vehicle profile on unplug, which reconciliation handles: the bridge
caches the retained template topic and the desired values are re-applied
whenever they differ.  The template write and a chargemode set in the same
command are still spaced ``_INTER_PUBLISH_DELAY_S`` apart because the
chargemode handler also rewrites the template internally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiomqtt

from ...core.config import MqttConfig
from ...core.models import DeviceCommand, DeviceRole, DeviceState

_log = logging.getLogger(__name__)

_MODE_THRESHOLD_W = 500.0
_RECONNECT_DELAY_S = 30
# Pause between successive simpleAPI set publishes — see module docstring.
_INTER_PUBLISH_DELAY_S = 2.0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return None if f != f else f  # guard NaN
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "on", "yes")


# Read topic reports long mode names; the set topic expects short ones.
_MODE_LONG_TO_SHORT = {
    "instant_charging": "instant",
    "pv_charging": "pv",
    "eco_charging": "eco",
    "scheduled_charging": "target",
}


def _normalize_mode(mode: str | None) -> str | None:
    """Map a read-topic chargemode payload to its set-topic equivalent."""
    if mode is None:
        return None
    return _MODE_LONG_TO_SHORT.get(mode, mode)


# ---------------------------------------------------------------------------
# Shared bridge — one MQTT connection per (host, port, root_topic)
# ---------------------------------------------------------------------------


class OpenWBMqttBridge:
    """Maintains one MQTT connection to the openWB broker and caches all
    ``{root}/simpleAPI/chargepoint/#`` values (retained + live updates).

    Started lazily from the first ``ensure_started()`` call so construction
    stays synchronous (device factories are not async).  Reconnects forever
    on failure.
    """

    def __init__(self, cfg: MqttConfig, root_topic: str) -> None:
        self._cfg = cfg
        self._root = root_topic.rstrip("/")
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None
        # cache[(chargepoint_id, key)] = (payload, monotonic timestamp)
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    @property
    def root_topic(self) -> str:
        return self._root

    @property
    def connected(self) -> bool:
        return self._client is not None

    def ensure_started(self) -> None:
        """Start the background connection task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"openwb_mqtt_bridge_{self._root}"
            )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def value(
        self, chargepoint_id: str, key: str, max_age_s: float | None = None
    ) -> str | None:
        """Return the cached payload for a chargepoint read topic, or None
        when absent or older than *max_age_s*."""
        entry = self._cache.get((chargepoint_id, key))
        if entry is None:
            return None
        payload, ts = entry
        if max_age_s is not None and (time.monotonic() - ts) > max_age_s:
            return None
        return payload

    async def publish_set(self, chargepoint_id: str, key: str, payload: str) -> None:
        """Publish to ``{root}/simpleAPI/set/chargepoint/{id}/{key}``.

        Raises when not connected — callers log and rely on the next control
        tick to retry.
        """
        client = self._client
        if client is None:
            raise ConnectionError(f"MQTT bridge {self._root!r} not connected")
        topic = f"{self._root}/simpleAPI/set/chargepoint/{chargepoint_id}/{key}"
        await client.publish(topic, payload)

    async def publish_charge_template(self, chargepoint_id: str, template_json: str) -> None:
        """Publish a full charge template to
        ``{root}/set/chargepoint/{id}/set/charge_template`` (the topic the
        openWB UI itself uses — atomic and correctly per-chargepoint)."""
        client = self._client
        if client is None:
            raise ConnectionError(f"MQTT bridge {self._root!r} not connected")
        topic = f"{self._root}/set/chargepoint/{chargepoint_id}/set/charge_template"
        await client.publish(topic, template_json)

    # ------------------------------------------------------------------
    # Internal connection loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._client = None
                _log.warning(
                    "openWB MQTT bridge %r disconnected: %s — reconnecting in %ds",
                    self._root, exc, _RECONNECT_DELAY_S,
                )
                await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _connect_and_listen(self) -> None:
        kwargs: dict[str, Any] = {"hostname": self._cfg.host, "port": self._cfg.port}
        if self._cfg.username:
            kwargs["username"] = self._cfg.username
        if self._cfg.password:
            kwargs["password"] = self._cfg.password

        async with aiomqtt.Client(**kwargs) as client:
            await client.subscribe(f"{self._root}/simpleAPI/chargepoint/#")
            # Retained per-chargepoint temporary charge template — needed for
            # the atomic template writes (current / SoC limit).
            await client.subscribe(f"{self._root}/chargepoint/+/set/charge_template")
            self._client = client
            _log.info(
                "openWB MQTT bridge connected to %s:%d (root=%r)",
                self._cfg.host, self._cfg.port, self._root,
            )
            try:
                async for message in client.messages:
                    self._store(str(message.topic), message.payload)
            finally:
                self._client = None

    def _store(self, topic: str, payload: Any) -> None:
        text = payload.decode() if isinstance(payload, (bytes, bytearray)) else str(payload)

        # {root}/simpleAPI/chargepoint/{id}/{key...}
        prefix = f"{self._root}/simpleAPI/chargepoint/"
        if topic.startswith(prefix):
            rest = topic[len(prefix):].split("/", 1)
            if len(rest) == 2:
                cp_id, key = rest
                self._cache[(cp_id, key)] = (text, time.monotonic())
            return

        # {root}/chargepoint/{id}/set/charge_template → cached under the
        # synthetic key "charge_template"
        prefix = f"{self._root}/chargepoint/"
        suffix = "/set/charge_template"
        if topic.startswith(prefix) and topic.endswith(suffix):
            cp_id = topic[len(prefix):-len(suffix)]
            if cp_id and "/" not in cp_id:
                self._cache[(cp_id, "charge_template")] = (text, time.monotonic())


# Shared bridges keyed by (host, port, root_topic) so multiple chargepoints
# on the same wallbox reuse one connection.
_bridges: dict[tuple[str, int, str], OpenWBMqttBridge] = {}


def get_bridge(cfg: MqttConfig, root_topic: str) -> OpenWBMqttBridge:
    key = (cfg.host, cfg.port, root_topic.rstrip("/"))
    bridge = _bridges.get(key)
    if bridge is None:
        bridge = OpenWBMqttBridge(cfg, root_topic)
        _bridges[key] = bridge
    return bridge


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------


class OpenWBMqttDevice:
    """Device plugin for one openWB chargepoint via MQTT simpleAPI.

    Parameters
    ----------
    device_id:
        Stable identifier for this chargepoint.
    bridge:
        Shared ``OpenWBMqttBridge`` (injected — one per broker/root topic).
    chargepoint_id:
        Numeric chargepoint id as it appears in the simpleAPI topics.
    phases, voltage_v:
        Used to convert the planned watts into the instant charging current.
    min_current_a, max_current_a:
        Clamp range for the instant charging current (simpleAPI allows 6–32).
    mode_instant, mode_pv, mode_stop:
        chargemode payloads; simpleAPI defaults are instant/pv/stop.

    Liveness: openWB publishes the read topics retained and ON CHANGE only —
    a parked, plugged-in car may not update plug_state / soc for hours, so
    cache age is NOT a valid staleness signal.  Availability is tied to the
    MQTT connection instead: while the bridge is connected, the broker's
    retained state is authoritative; on disconnect the device reports
    unavailable until the bridge reconnects and the retained values reload.
    """

    def __init__(
        self,
        device_id: str,
        bridge: OpenWBMqttBridge,
        *,
        chargepoint_id: int | str,
        phases: int = 3,
        voltage_v: float = 230.0,
        min_current_a: int = 6,
        max_current_a: int = 16,
        mode_instant: str = "instant",
        mode_pv: str = "pv",
        mode_stop: str = "stop",
    ) -> None:
        self._device_id = device_id
        self._bridge = bridge
        self._cp_id = str(chargepoint_id)
        self._phases = phases
        self._voltage_v = voltage_v
        self._min_current_a = min_current_a
        self._max_current_a = max_current_a
        self._mode_instant = mode_instant
        self._mode_pv = mode_pv
        self._mode_stop = mode_stop
        self._target_soc_pct: float | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return DeviceRole.EV_CHARGER

    def update_target_soc(self, soc_pct: float | None) -> None:
        """Store the active goal's target SoC — written as the instant
        charging SoC limit when instant mode is activated."""
        self._target_soc_pct = soc_pct

    # ------------------------------------------------------------------
    # Device protocol
    # ------------------------------------------------------------------

    async def get_state(self) -> DeviceState:
        """Return the latest cached simpleAPI values for this chargepoint."""
        self._bridge.ensure_started()

        # Vehicle SoC lives in the nested soc/soc topic; pro_soc (direct
        # chargepoint reading, Pro hardware only) is the fallback.
        soc_pct = _to_float(self._value("soc/soc"))
        if soc_pct is None:
            soc_pct = _to_float(self._value("pro_soc"))
        power_w = _to_float(self._value("power"))
        plug_raw = self._value("plug_state")
        plugged = _to_bool(plug_raw) if plug_raw is not None else False
        mode = _normalize_mode(self._value("chargemode"))

        available = self._bridge.connected and plugged and soc_pct is not None
        return DeviceState(
            device_id=self._device_id,
            power_w=power_w,
            soc_pct=soc_pct,
            available=available,
            extra={"plugged": plugged, "chargemode": mode},
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """Translate a ``set_power_w`` sentinel into simpleAPI set topics."""
        if command.command != "set_power_w":
            return
        self._bridge.ensure_started()

        value = float(command.value) if command.value is not None else 0.0
        if value > _MODE_THRESHOLD_W:
            mode = self._mode_instant
        elif value > 0.0:
            mode = self._mode_pv
        else:
            mode = self._mode_stop

        current_mode = _normalize_mode(self._value("chargemode"))

        # Collect all needed publishes, instant parameters *before* the mode
        # switch so openWB never starts instant charging with stale current /
        # limit values.  ("template", …) entries carry a full charge-template
        # JSON; everything else is a simpleAPI set key.
        pending: list[tuple[str, str, str]] = []  # (kind/key, payload, log message)
        if mode == self._mode_instant:
            pending += self._instant_template_publishes(value)
        if current_mode != mode:
            pending.append((
                "chargemode", mode,
                f"chargemode {current_mode!r} → {mode!r} (value={value:.0f} W)",
            ))

        # Publish spaced apart — openWB rewrites the template internally for
        # chargemode sets too, and back-to-back writes race and get reverted.
        for i, (key, payload, log_msg) in enumerate(pending):
            if i:
                await asyncio.sleep(_INTER_PUBLISH_DELAY_S)
            try:
                if key == "template":
                    await self._bridge.publish_charge_template(self._cp_id, payload)
                else:
                    await self._bridge.publish_set(self._cp_id, key, payload)
                _log.info("OpenWBMqttDevice %r: %s", self._device_id, log_msg)
            except Exception:
                _log.warning(
                    "OpenWBMqttDevice %r: failed to publish %s",
                    self._device_id, key, exc_info=True,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _value(self, key: str) -> str | None:
        return self._bridge.value(self._cp_id, key)

    def _instant_template_publishes(self, value_w: float) -> list[tuple[str, str, str]]:
        """Return the template publish (if any) that brings the instant
        charging current and SoC limit in line with the plan.

        Reconciles against the cached retained charge template — the same
        JSON openWB regulates on — and returns a single atomic full-template
        write when anything differs.  (The per-id simpleAPI set topics for
        these values are broken: they always write to the lowest chargepoint.)
        """
        raw = self._bridge.value(self._cp_id, "charge_template")
        if raw is None:
            _log.debug(
                "OpenWBMqttDevice %r: no charge template received yet — "
                "cannot set charging current / SoC limit this tick",
                self._device_id,
            )
            return []
        try:
            template = json.loads(raw)
            instant = template["chargemode"]["instant_charging"]
        except (ValueError, KeyError, TypeError):
            _log.warning(
                "OpenWBMqttDevice %r: unparseable charge template — "
                "cannot set charging current / SoC limit",
                self._device_id, exc_info=True,
            )
            return []

        target_a = max(
            self._min_current_a,
            min(self._max_current_a, round(value_w / (self._voltage_v * self._phases))),
        )
        changes: list[str] = []
        if instant.get("current") != target_a:
            instant["current"] = target_a
            changes.append(f"current → {target_a} A ({value_w:.0f} W / {self._phases} phases)")

        if self._target_soc_pct is not None:
            limit_soc = int(round(self._target_soc_pct))
            limit = instant.setdefault("limit", {})
            if limit.get("selected") != "soc":
                limit["selected"] = "soc"
                changes.append("limit type → soc")
            if limit.get("soc") != limit_soc:
                limit["soc"] = limit_soc
                changes.append(f"SoC limit → {limit_soc}%")

        if not changes:
            return []
        return [(
            "template", json.dumps(template),
            "charge template: " + ", ".join(changes),
        )]

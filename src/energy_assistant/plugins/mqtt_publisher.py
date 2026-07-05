"""MQTT Discovery publisher for EV charging entities.

Publishes one HA device per EV chargepoint (keyed by asset_id) containing:
  - sensor:        soc, charging_mode, target_soc, deadline
  - binary_sensor: override_active
  - number:        target_soc_set  (writable → set override SoC)
  - datetime:      deadline_set    (writable → set override deadline, local tz)
  - button:        clear_override

Command topics are subscribed by the addon. When HA sends a command the
publisher resolves the full (soc, deadline) pair from cached goal state
and calls the on_set_target / on_clear_target callbacks into the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import aiomqtt

from ..assets.ev import EvChargingAsset, EvChargingGoal
from ..core.config import MqttConfig
from ..core.models import DeviceState

_log = logging.getLogger(__name__)

_BASE = "energy_assistant/ev"
_DISC = "homeassistant"
_RECONNECT_DELAY_S = 30


class EvMqttPublisher:
    """Manages MQTT Discovery registration and state publishing for EV assets."""

    def __init__(
        self,
        cfg: MqttConfig,
        assets: list[EvChargingAsset],
        on_set_target: Callable[[str, float, datetime], Awaitable[None]],
        on_clear_target: Callable[[str], Awaitable[None]],
    ) -> None:
        self._cfg = cfg
        self._assets = assets
        self._on_set_target = on_set_target
        self._on_clear_target = on_clear_target
        # Outbound publish queue: (topic, payload, retain)
        self._queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        # Last known goal per asset — used to fill defaults on partial commands
        self._cached_goals: dict[str, EvChargingGoal] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mqtt_publisher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # ------------------------------------------------------------------
    # Called by the planning loop after each optimizer cycle
    # ------------------------------------------------------------------

    async def publish_states(
        self,
        goals: list[EvChargingGoal],
        device_states: dict[str, DeviceState],
        overrides: dict[str, tuple[float, datetime]],
    ) -> None:
        goals_by_asset = {g.asset_id: g for g in goals}
        self._cached_goals = dict(goals_by_asset)
        for asset in self._assets:
            goal = goals_by_asset.get(asset.asset_id)
            state = device_states.get(asset.device_id)
            for topic, payload, retain in _build_state_messages(asset, goal, state, overrides):
                await self._queue.put((topic, payload, retain))

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("MQTT disconnected: %s — reconnecting in %ds", exc, _RECONNECT_DELAY_S)
                await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _connect_and_run(self) -> None:
        kwargs: dict[str, Any] = {"hostname": self._cfg.host, "port": self._cfg.port}
        if self._cfg.username:
            kwargs["username"] = self._cfg.username
        if self._cfg.password:
            kwargs["password"] = self._cfg.password

        async with aiomqtt.Client(**kwargs) as client:
            _log.info("MQTT connected to %s:%d", self._cfg.host, self._cfg.port)
            await self._publish_discovery(client)
            for asset in self._assets:
                slug = asset.asset_id
                await client.subscribe(f"{_BASE}/{slug}/target_soc/set")
                await client.subscribe(f"{_BASE}/{slug}/deadline/set")
                await client.subscribe(f"{_BASE}/{slug}/clear_override/press")

            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._drain_queue(client))
                tg.create_task(self._handle_messages(client))

    async def _drain_queue(self, client: aiomqtt.Client) -> None:
        while True:
            topic, payload, retain = await self._queue.get()
            try:
                await client.publish(topic, payload, retain=retain)
            except Exception as exc:
                _log.warning("MQTT publish error topic=%r: %s", topic, exc)
            finally:
                self._queue.task_done()

    async def _handle_messages(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            payload = (
                message.payload.decode()
                if isinstance(message.payload, bytes)
                else str(message.payload)
            )
            try:
                await self._dispatch(topic, payload)
            except Exception as exc:
                _log.warning("MQTT command error topic=%r: %s", topic, exc)

    async def _dispatch(self, topic: str, payload: str) -> None:
        # topic: energy_assistant/ev/{asset_id}/{cmd}/set  or  .../press
        parts = topic.split("/")
        if len(parts) < 4:
            return
        asset_id = parts[2]
        cmd = parts[3]
        asset = next((a for a in self._assets if a.asset_id == asset_id), None)
        if asset is None:
            _log.warning("MQTT command for unknown asset: %r", asset_id)
            return

        if cmd == "clear_override":
            _log.info("MQTT clear_override: %r", asset_id)
            await self._on_clear_target(asset_id)

        elif cmd == "target_soc":
            try:
                soc = float(payload)
            except ValueError:
                _log.warning("MQTT target_soc invalid payload: %r", payload)
                return
            deadline = self._resolve_deadline(asset_id, asset)
            _log.info("MQTT set_target: %r → %.0f%% by %s", asset_id, soc, deadline)
            await self._on_set_target(asset_id, soc, deadline)

        elif cmd == "deadline":
            # HA datetime entity sends YYYY-MM-DDTHH:MM:SS (no tz) — treat as asset local time
            try:
                dt = datetime.fromisoformat(payload)
                if dt.tzinfo is None:
                    tz = ZoneInfo(asset.timezone)
                    dt = dt.replace(tzinfo=tz)
                dt_utc = dt.astimezone(timezone.utc)
            except (ValueError, KeyError):
                _log.warning("MQTT deadline invalid payload: %r", payload)
                return
            soc = self._resolve_target_soc(asset_id, asset)
            _log.info("MQTT set_deadline: %r → %s (soc=%.0f%%)", asset_id, dt_utc, soc)
            await self._on_set_target(asset_id, soc, dt_utc)

    def _resolve_deadline(self, asset_id: str, asset: EvChargingAsset) -> datetime:
        """Fall back to cached goal deadline, or 24 h from now."""
        goal = self._cached_goals.get(asset_id)
        if goal is not None:
            return goal.target_by
        return datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)

    def _resolve_target_soc(self, asset_id: str, asset: EvChargingAsset) -> float:
        """Fall back to cached goal target SoC, or asset charge limit."""
        goal = self._cached_goals.get(asset_id)
        if goal is not None:
            return goal.target_soc_pct
        return asset.charge_limit_soc_pct

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        for asset in self._assets:
            device = {
                "identifiers": [f"energy_assistant_ev_{asset.asset_id}"],
                "name": asset.label,
                "manufacturer": "Energy Assistant",
                "model": "EV Charger",
            }
            for config_topic, config_payload in _discovery_configs(asset, device):
                await client.publish(config_topic, json.dumps(config_payload), retain=True)
        _log.info("MQTT Discovery published for %d EV asset(s)", len(self._assets))


# ------------------------------------------------------------------
# Pure helpers — Discovery config generation
# ------------------------------------------------------------------

def _discovery_configs(
    asset: EvChargingAsset, device: dict
) -> list[tuple[str, dict]]:
    slug = asset.asset_id
    base = f"{_BASE}/{slug}"
    avail = f"{base}/available"
    configs: list[tuple[str, dict]] = []

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_soc/config",
        {
            "unique_id": f"ea_ev_{slug}_soc",
            "name": "State of Charge",
            "device": device,
            "state_topic": f"{base}/soc",
            "availability_topic": avail,
            "unit_of_measurement": "%",
            "device_class": "battery",
            "state_class": "measurement",
        },
    ))

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_charging_mode/config",
        {
            "unique_id": f"ea_ev_{slug}_charging_mode",
            "name": "Charging Mode",
            "device": device,
            "state_topic": f"{base}/charging_mode",
            "availability_topic": avail,
            "icon": "mdi:ev-station",
        },
    ))

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_target_soc/config",
        {
            "unique_id": f"ea_ev_{slug}_target_soc",
            "name": "Target SoC",
            "device": device,
            "state_topic": f"{base}/target_soc",
            "availability_topic": avail,
            "unit_of_measurement": "%",
            "icon": "mdi:battery-arrow-up",
        },
    ))

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_deadline/config",
        {
            "unique_id": f"ea_ev_{slug}_deadline",
            "name": "Charging Deadline",
            "device": device,
            "state_topic": f"{base}/deadline",
            "availability_topic": avail,
            "device_class": "timestamp",
        },
    ))

    configs.append((
        f"{_DISC}/binary_sensor/ea_ev_{slug}_override/config",
        {
            "unique_id": f"ea_ev_{slug}_override",
            "name": "Override Active",
            "device": device,
            "state_topic": f"{base}/override_active",
            "availability_topic": avail,
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:pencil-circle",
        },
    ))

    configs.append((
        f"{_DISC}/number/ea_ev_{slug}_target_soc_set/config",
        {
            "unique_id": f"ea_ev_{slug}_target_soc_set",
            "name": "Set Target SoC",
            "device": device,
            "state_topic": f"{base}/target_soc_set/state",
            "command_topic": f"{base}/target_soc/set",
            "availability_topic": avail,
            "unit_of_measurement": "%",
            "min": 0,
            "max": 100,
            "step": 5,
            "mode": "slider",
            "icon": "mdi:battery-arrow-up-outline",
        },
    ))

    configs.append((
        f"{_DISC}/datetime/ea_ev_{slug}_deadline_set/config",
        {
            "unique_id": f"ea_ev_{slug}_deadline_set",
            "name": "Set Deadline",
            "device": device,
            "state_topic": f"{base}/deadline_set/state",
            "command_topic": f"{base}/deadline/set",
            "availability_topic": avail,
            "icon": "mdi:clock-end",
        },
    ))

    configs.append((
        f"{_DISC}/button/ea_ev_{slug}_clear_override/config",
        {
            "unique_id": f"ea_ev_{slug}_clear_override",
            "name": "Clear Override",
            "device": device,
            "command_topic": f"{base}/clear_override/press",
            "availability_topic": avail,
            "icon": "mdi:close-circle-outline",
        },
    ))

    return configs


# ------------------------------------------------------------------
# Pure helpers — State message generation
# ------------------------------------------------------------------

def _build_state_messages(
    asset: EvChargingAsset,
    goal: EvChargingGoal | None,
    state: DeviceState | None,
    overrides: dict[str, tuple[float, datetime]],
) -> list[tuple[str, str, bool]]:
    """Return list of (topic, payload, retain) for one asset."""
    slug = asset.asset_id
    base = f"{_BASE}/{slug}"
    connected = state is not None and state.available
    msgs: list[tuple[str, str, bool]] = []

    msgs.append((f"{base}/available", "online" if connected else "offline", True))

    if state is not None and state.soc_pct is not None:
        msgs.append((f"{base}/soc", f"{state.soc_pct:.0f}", True))

    msgs.append((f"{base}/charging_mode", _derive_mode(goal, state), True))
    msgs.append((f"{base}/override_active", "ON" if asset.asset_id in overrides else "OFF", True))

    if goal is not None:
        msgs.append((f"{base}/target_soc", f"{goal.target_soc_pct:.0f}", True))
        msgs.append((f"{base}/deadline", goal.target_by.isoformat(), True))
        msgs.append((f"{base}/target_soc_set/state", f"{goal.target_soc_pct:.0f}", True))
        # HA datetime entity expects YYYY-MM-DDTHH:MM:SS — send as asset local time
        try:
            tz = ZoneInfo(asset.timezone)
            local_dt = goal.target_by.astimezone(tz)
        except Exception:
            local_dt = goal.target_by
        msgs.append((f"{base}/deadline_set/state", local_dt.strftime("%Y-%m-%dT%H:%M:%S"), True))

    return msgs


def _derive_mode(goal: EvChargingGoal | None, state: DeviceState | None) -> str:
    if state is None or not state.available:
        return "disconnected"
    soc = state.soc_pct or 0.0
    if goal is None:
        return "pv_charging"
    if soc >= goal.target_soc_pct:
        return "stop"
    now = datetime.now(timezone.utc)
    if goal.phase2_required_kwh > 0.01 and now >= goal.phase2_start_time:
        return "instant_charging"
    if soc >= goal.charge_limit_soc_pct:
        return "stop"
    return "pv_charging"

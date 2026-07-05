"""MQTT Discovery publisher for EV charging entities.

Publishes one HA device per EV chargepoint (keyed by asset_id) containing:
  - sensor:        soc, charging_mode, target_soc, deadline  (read-only)
  - binary_sensor: override_active                           (read-only)
  - number:        target_soc_set   (writable — stages SoC)
  - date:          deadline_date    (writable — stages date, asset local tz)
  - time:          deadline_time    (writable — stages time, asset local tz)
  - switch:        override_active  (ON = apply staged, OFF = revert to schedule)

Setting the number/date/time pickers updates staged values only — the optimizer
is not affected until the switch is turned ON. Turning the switch OFF reverts
the optimizer to the schedule but keeps the staged values intact so the user
can re-enable with the same settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
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
        on_stage: Callable[[str, float, datetime], Awaitable[None]],
        on_enable_override: Callable[[str], Awaitable[None]],
        on_disable_override: Callable[[str], Awaitable[None]],
    ) -> None:
        self._cfg = cfg
        self._assets = assets
        self._on_stage = on_stage
        self._on_enable_override = on_enable_override
        self._on_disable_override = on_disable_override
        self._queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        # Cached staged values per asset — updated on each publish_states call.
        # Used to fill the other half when only date or only time changes.
        self._cached_staged: dict[str, tuple[float, datetime]] = {}

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
        staged: dict[str, tuple[float, datetime]],
    ) -> None:
        self._cached_staged = dict(staged)
        goals_by_asset = {g.asset_id: g for g in goals}
        for asset in self._assets:
            goal = goals_by_asset.get(asset.asset_id)
            state = device_states.get(asset.device_id)
            staged_for_asset = staged.get(asset.asset_id)
            override_active = asset.asset_id in overrides
            for topic, payload, retain in _build_state_messages(
                asset, goal, state, staged_for_asset, override_active
            ):
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
                await client.subscribe(f"{_BASE}/{slug}/deadline_date/set")
                await client.subscribe(f"{_BASE}/{slug}/deadline_time/set")
                await client.subscribe(f"{_BASE}/{slug}/override/set")

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
        # energy_assistant/ev/{asset_id}/{cmd}/set
        parts = topic.split("/")
        if len(parts) < 5:
            return
        asset_id = parts[2]
        cmd = parts[3]
        asset = next((a for a in self._assets if a.asset_id == asset_id), None)
        if asset is None:
            _log.warning("MQTT command for unknown asset: %r", asset_id)
            return

        tz = ZoneInfo(asset.timezone)

        if cmd == "override":
            if payload.upper() in ("ON", "TRUE", "1"):
                _log.info("MQTT override ON: %r", asset_id)
                await self._on_enable_override(asset_id)
            else:
                _log.info("MQTT override OFF: %r", asset_id)
                await self._on_disable_override(asset_id)

        elif cmd == "target_soc":
            try:
                soc = float(payload)
            except ValueError:
                _log.warning("MQTT target_soc invalid payload: %r", payload)
                return
            _, deadline = self._resolve_staged(asset_id, asset, tz)
            _log.info("MQTT stage target_soc: %r → %.0f%% by %s", asset_id, soc, deadline)
            await self._on_stage(asset_id, soc, deadline)

        elif cmd == "deadline_date":
            # HA date entity sends YYYY-MM-DD
            try:
                new_date = date.fromisoformat(payload)
            except ValueError:
                _log.warning("MQTT deadline_date invalid payload: %r", payload)
                return
            soc, current_deadline = self._resolve_staged(asset_id, asset, tz)
            local_deadline = current_deadline.astimezone(tz)
            new_local = datetime(
                new_date.year, new_date.month, new_date.day,
                local_deadline.hour, local_deadline.minute, 0,
                tzinfo=tz,
            )
            dt_utc = new_local.astimezone(timezone.utc)
            _log.info("MQTT stage deadline_date: %r → %s (soc=%.0f%%)", asset_id, dt_utc, soc)
            await self._on_stage(asset_id, soc, dt_utc)

        elif cmd == "deadline_time":
            # HA time entity sends HH:MM:SS in local time
            try:
                new_time = time.fromisoformat(payload)
            except ValueError:
                _log.warning("MQTT deadline_time invalid payload: %r", payload)
                return
            soc, current_deadline = self._resolve_staged(asset_id, asset, tz)
            local_deadline = current_deadline.astimezone(tz)
            new_local = datetime(
                local_deadline.year, local_deadline.month, local_deadline.day,
                new_time.hour, new_time.minute, 0,
                tzinfo=tz,
            )
            # If new time has already passed today, advance to tomorrow
            if new_local <= datetime.now(tz):
                new_local += timedelta(days=1)
            dt_utc = new_local.astimezone(timezone.utc)
            _log.info("MQTT stage deadline_time: %r → %s (soc=%.0f%%)", asset_id, dt_utc, soc)
            await self._on_stage(asset_id, soc, dt_utc)

    def _resolve_staged(
        self, asset_id: str, asset: EvChargingAsset, tz: ZoneInfo
    ) -> tuple[float, datetime]:
        """Return (soc, deadline) from cached staged values, or sensible defaults."""
        if asset_id in self._cached_staged:
            return self._cached_staged[asset_id]
        # Default: charge_limit SoC, next 06:00 local
        tomorrow = datetime.now(tz) + timedelta(days=1)
        default_deadline = tomorrow.replace(hour=6, minute=0, second=0, microsecond=0)
        return asset.charge_limit_soc_pct, default_deadline.astimezone(timezone.utc)

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
        f"{_DISC}/binary_sensor/ea_ev_{slug}_override_active/config",
        {
            "unique_id": f"ea_ev_{slug}_override_active",
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
            "state_topic": f"{base}/staged_soc/state",
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
        f"{_DISC}/date/ea_ev_{slug}_deadline_date/config",
        {
            "unique_id": f"ea_ev_{slug}_deadline_date",
            "name": "Deadline Date",
            "device": device,
            "state_topic": f"{base}/staged_date/state",
            "command_topic": f"{base}/deadline_date/set",
            "availability_topic": avail,
            "icon": "mdi:calendar-clock",
        },
    ))

    configs.append((
        f"{_DISC}/time/ea_ev_{slug}_deadline_time/config",
        {
            "unique_id": f"ea_ev_{slug}_deadline_time",
            "name": "Deadline Time",
            "device": device,
            "state_topic": f"{base}/staged_time/state",
            "command_topic": f"{base}/deadline_time/set",
            "availability_topic": avail,
            "icon": "mdi:clock-end",
        },
    ))

    configs.append((
        f"{_DISC}/switch/ea_ev_{slug}_override/config",
        {
            "unique_id": f"ea_ev_{slug}_override",
            "name": "Override Active",
            "device": device,
            "state_topic": f"{base}/override_active",
            "command_topic": f"{base}/override/set",
            "availability_topic": avail,
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:flash",
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
    staged: tuple[float, datetime] | None,
    override_active: bool,
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
    msgs.append((f"{base}/override_active", "ON" if override_active else "OFF", True))

    if goal is not None:
        msgs.append((f"{base}/target_soc", f"{goal.target_soc_pct:.0f}", True))
        msgs.append((f"{base}/deadline", goal.target_by.isoformat(), True))

    # Staged values drive the setter entity states (date/time/soc pickers)
    if staged is not None:
        soc, deadline_utc = staged
        msgs.append((f"{base}/staged_soc/state", f"{soc:.0f}", True))
        try:
            tz = ZoneInfo(asset.timezone)
            local_dt = deadline_utc.astimezone(tz)
        except Exception:
            local_dt = deadline_utc
        msgs.append((f"{base}/staged_date/state", local_dt.strftime("%Y-%m-%d"), True))
        msgs.append((f"{base}/staged_time/state", local_dt.strftime("%H:%M:%S"), True))

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

"""MQTT Discovery publisher for EV charging entities.

Publishes one HA device per EV chargepoint (keyed by asset_id) containing:
  - sensor:        soc, range, charging_state, charging_mode, target_soc,
                   deadline (read-only)
  - binary_sensor: override_active, plugged                  (read-only)
  - number:        target_soc_set   (writable — stages SoC)
  - date:          deadline_date    (writable — stages date, asset local tz)
  - time:          deadline_time    (writable — stages time, asset local tz)
  - switch:        override_active  (ON = apply staged, OFF = revert to schedule)
  - weekly plan, per weekday (config entities, written straight to the plan):
      number  weekly_{day}_soc      target SoC
      time    weekly_{day}_time     departure time (asset local tz)
      switch  weekly_{day}_enabled  whether the day has a target at all

Setting the number/date/time pickers updates staged values only — the optimizer
is not affected until the switch is turned ON. Turning the switch OFF reverts
the optimizer to the schedule but keeps the staged values intact so the user
can re-enable with the same settings.

Availability is deliberately NOT tied to whether the car is plugged in:
  - ``energy_assistant/status`` (LWT) — Energy Assistant is running; gates
    every entity.
  - ``energy_assistant/ev/{asset_id}/available`` — chargepoint data (SoC) is
    readable; additionally gates the SoC and plug sensors.
Plug state is exposed separately via the ``plugged`` binary_sensor.
  - ``energy_assistant/ev/{asset_id}/range_available`` — the chargepoint
    reports a vehicle range (``DeviceState.extra["range_km"]``); gates the
    range sensor, so a car without range data shows "unavailable".

Live readings (``publish_readings``, every control tick, published only on
change and re-published on every reconnect): ``available``, ``soc``,
``range_available``, ``range``, ``plugged``.  Goal / override / staged values
are published after each planning cycle (``publish_states``).

Live charging status (``publish_live_states``, every control tick, published
only on change and re-published on every reconnect):
  - ``charging_state`` — the active plan slot's mode, exactly what the UI
    shows: ``charge_from_pv`` | ``charge_from_grid`` | ``charge_phase2`` |
    ``idle`` | ``force_charging`` | ``disabled``
    (see ``core.models.ev_current_mode``).
  - ``charging_mode`` — the openWB mode Energy Assistant currently commands:
    ``instant_charging`` | ``pv_charging`` | ``stop``, or ``disconnected``
    (car not connected, nothing sent) / ``disabled`` (chargepoint disabled,
    hands off).  See the sentinel encoding in ``assets/ev.py``.

Price curve
-----------
Two sensors on the "Energy Assistant" device, fed from one retained topic
after every planning cycle (``publish_price_curve``):

  - ``sensor.energy_assistant_electricity_price`` — current import price; its
    attributes hold the whole curve (``columns`` + ``data`` rows of
    ``[epoch_ms, price, effective, feed_in, estimated]``) for ApexCharts.
  - ``sensor.energy_assistant_effective_electricity_price`` — current
    effective price (see ``core/price_curve.py``).

Savings KPIs
------------
With ``savings_kpis=True`` one more HA device, "Energy Assistant", carries
cumulative monetary totals (``state_class: total``) — predicted savings, real
savings and their difference.  Home Assistant's long-term statistics turn
those into any period (utility meter, statistics cards).  See
``core/savings.py`` for how the numbers are computed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import aiomqtt

from ..assets.ev import EvChargingAsset, EvChargingGoal, EvWeeklyTarget, default_weekly_target
from ..core.config import MqttConfig
from ..core.models import EV_DISPLAY_MODES, DeviceState
from ..core.price_curve import PriceSlot, current_slot, price_curve_payload

_log = logging.getLogger(__name__)

_BASE = "energy_assistant/ev"
_DISC = "homeassistant"
_STATUS_TOPIC = "energy_assistant/status"
_SAVINGS_TOPIC = "energy_assistant/kpi/savings"
_PRICE_TOPIC = "energy_assistant/prices/state"
_PRICE_ATTR_TOPIC = "energy_assistant/prices/curve"
# The site-wide "Energy Assistant" device shared by KPI and price sensors.
_SITE_DEVICE = {
    "identifiers": ["energy_assistant_kpis"],
    "name": "Energy Assistant",
    "manufacturer": "Energy Assistant",
    "model": "KPIs",
}
# (key in the state payload, unique-id suffix, entity name)
_SAVINGS_SENSORS = (
    ("real", "savings_real", "Real savings"),
    ("predicted", "savings_predicted", "Predicted savings"),
    ("deviation", "savings_deviation", "Savings deviation"),
    ("predicted_latest", "savings_predicted_last_plan", "Predicted savings (last plan)"),
)
_RECONNECT_DELAY_S = 30
CHARGING_MODES = ("instant_charging", "pv_charging", "stop", "disconnected", "disabled")
# Sentinel boundary between PV and Instant mode — see assets/ev.py.
_MODE_THRESHOLD_W = 500.0
# ISO weekday (1=Mon … 7=Sun) → topic / unique_id key
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class EvMqttPublisher:
    """Manages MQTT Discovery registration and state publishing for EV assets."""

    def __init__(
        self,
        cfg: MqttConfig,
        assets: list[EvChargingAsset],
        on_stage: Callable[[str, float, datetime], Awaitable[None]],
        on_enable_override: Callable[[str], Awaitable[None]],
        on_disable_override: Callable[[str], Awaitable[None]],
        on_set_weekly: Callable[[str, int, dict[str, Any]], Awaitable[None]] | None = None,
        savings_kpis: bool = False,
    ) -> None:
        self._savings_kpis = savings_kpis
        # Last published KPI payload — re-published on every (re)connect.
        self._savings_payload: str | None = None
        # Last price curve — attributes re-published on reconnect; the state
        # topic goes through _live_states like the other live values.
        self._price_slots: list[PriceSlot] = []
        self._price_attrs: str | None = None
        self._cfg = cfg
        self._assets = assets
        self._on_stage = on_stage
        self._on_enable_override = on_enable_override
        self._on_disable_override = on_disable_override
        self._on_set_weekly = on_set_weekly
        # Latest weekly plans per asset — re-published on every (re)connect.
        self._weekly_plans: dict[str, dict[int, EvWeeklyTarget]] = {}
        self._queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        # Cached staged values per asset — updated on each publish_states call.
        # Used to fill the other half when only date or only time changes.
        self._cached_staged: dict[str, tuple[float, datetime]] = {}
        # Last live-state payload per topic — dedups the per-tick publishes
        # and is re-published on every (re)connect.
        self._live_states: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mqtt_publisher")

    async def stop(self) -> None:
        # A clean disconnect does not fire the LWT — mark offline explicitly.
        # Best effort: drained by the publish loop if still connected.
        await self._queue.put((_STATUS_TOPIC, "offline", True))
        try:
            await asyncio.wait_for(self._queue.join(), timeout=2)
        except asyncio.TimeoutError:
            pass
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

    async def publish_live_states(
        self, states: dict[str, tuple[str | None, str | None]]
    ) -> None:
        """Publish ``(charging_state, charging_mode)`` per asset_id.

        Called on every control tick; only changed values are queued.  A
        ``None`` entry leaves that topic untouched.
        """
        for asset_id, (charging_state, charging_mode) in states.items():
            base = f"{_BASE}/{asset_id}"
            await self._queue_if_changed([
                (f"{base}/charging_state", charging_state),
                (f"{base}/charging_mode", charging_mode),
            ])

    async def publish_readings(self, device_states: dict[str, DeviceState]) -> None:
        """Publish each EV's live readings (SoC, range, plug state).

        Called on every control tick with the latest polled states; only
        changed values are queued.
        """
        for asset in self._assets:
            await self._queue_if_changed(
                _reading_messages(asset, device_states.get(asset.device_id))
            )

    async def _queue_if_changed(self, msgs: list[tuple[str, str | None]]) -> None:
        """Queue retained messages whose payload differs from the last one
        sent (``None`` leaves the topic untouched)."""
        for topic, payload in msgs:
            if payload is None or self._live_states.get(topic) == payload:
                continue
            self._live_states[topic] = payload
            await self._queue.put((topic, payload, True))

    async def publish_weekly_plans(
        self, plans: dict[str, dict[int, EvWeeklyTarget]]
    ) -> None:
        """Publish every asset's weekly plan (call at startup and on change)."""
        self._weekly_plans = {aid: dict(rows) for aid, rows in plans.items()}
        for asset in self._assets:
            for msg in _weekly_state_messages(asset, self._weekly_plans.get(asset.asset_id, {})):
                await self._queue.put(msg)

    async def publish_savings(self, totals: dict[str, float]) -> None:
        """Publish the cumulative savings totals (€) — see ``_SAVINGS_SENSORS``."""
        if not self._savings_kpis:
            return
        self._savings_payload = json.dumps(
            {key: round(float(totals.get(key, 0.0)), 4) for key, _, _ in _SAVINGS_SENSORS}
        )
        await self._queue.put((_SAVINGS_TOPIC, self._savings_payload, True))

    async def publish_price_curve(self, slots: list[PriceSlot], step_minutes: int) -> None:
        """Publish the price curve — current prices as state, the curve as attributes."""
        self._price_slots = list(slots)
        attrs = json.dumps(price_curve_payload(slots, step_minutes), separators=(",", ":"))
        self._price_attrs = attrs
        # Attributes first, so the state update renders with the new curve.
        await self._queue.put((_PRICE_ATTR_TOPIC, attrs, True))
        await self.publish_current_price()

    async def publish_current_price(self) -> None:
        """Publish the current slot's prices (call on every control tick).

        Only queued when the slot's values change, so this is cheap.
        """
        if not self._price_slots:
            return
        cur = current_slot(self._price_slots, datetime.now(timezone.utc))
        state = json.dumps({
            "price": round(cur.import_price, 4) if cur else None,
            "effective": (
                round(cur.effective_price, 4)
                if cur and cur.effective_price is not None else None
            ),
        })
        await self._queue_if_changed([(_PRICE_TOPIC, state)])

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
        kwargs: dict[str, Any] = {
            "hostname": self._cfg.host,
            "port": self._cfg.port,
            "will": aiomqtt.Will(_STATUS_TOPIC, "offline", qos=1, retain=True),
        }
        if self._cfg.username:
            kwargs["username"] = self._cfg.username
        if self._cfg.password:
            kwargs["password"] = self._cfg.password

        async with aiomqtt.Client(**kwargs) as client:
            _log.info("MQTT connected to %s:%d", self._cfg.host, self._cfg.port)
            await self._publish_discovery(client)
            await client.publish(_STATUS_TOPIC, "online", qos=1, retain=True)
            if self._savings_payload is not None:
                await client.publish(_SAVINGS_TOPIC, self._savings_payload, retain=True)
            if self._price_attrs is not None:
                await client.publish(_PRICE_ATTR_TOPIC, self._price_attrs, retain=True)
            # Live states change rarely and are deduped, so the broker may
            # have lost them (restart without persistence) — re-send.
            for topic, payload in list(self._live_states.items()):
                await client.publish(topic, payload, retain=True)
            for asset in self._assets:
                slug = asset.asset_id
                await client.subscribe(f"{_BASE}/{slug}/target_soc/set")
                await client.subscribe(f"{_BASE}/{slug}/deadline_date/set")
                await client.subscribe(f"{_BASE}/{slug}/deadline_time/set")
                await client.subscribe(f"{_BASE}/{slug}/override/set")
                await client.subscribe(f"{_BASE}/{slug}/weekly/+/+/set")
                for topic, payload, retain in _weekly_state_messages(
                    asset, self._weekly_plans.get(slug, {})
                ):
                    await client.publish(topic, payload, retain=retain)

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

        if cmd == "weekly":
            # energy_assistant/ev/{asset_id}/weekly/{day}/{field}/set
            if len(parts) != 7 or self._on_set_weekly is None:
                return
            change = _parse_weekly_command(parts[4], parts[5], payload)
            if change is None:
                _log.warning("MQTT weekly command invalid: topic=%r payload=%r", topic, payload)
                return
            weekday, changes = change
            _log.info("MQTT weekly plan: %r weekday=%d %s", asset_id, weekday, changes)
            await self._on_set_weekly(asset_id, weekday, changes)
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
        for config_topic, config_payload in _price_discovery_configs():
            await client.publish(config_topic, json.dumps(config_payload), retain=True)
        if self._savings_kpis:
            for config_topic, config_payload in _savings_discovery_configs():
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
    # Bridge-only availability for entities that don't depend on live data
    # (goal, mode, setters); data-gated availability for the live readings.
    bridge_avail = {"availability_topic": _STATUS_TOPIC}
    data_avail = {
        "availability": [
            {"topic": _STATUS_TOPIC},
            {"topic": f"{base}/available"},
        ],
        "availability_mode": "all",
    }
    configs: list[tuple[str, dict]] = []

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_soc/config",
        {
            "unique_id": f"ea_ev_{slug}_soc",
            "name": "State of Charge",
            "device": device,
            "state_topic": f"{base}/soc",
            **data_avail,
            "unit_of_measurement": "%",
            "device_class": "battery",
            "state_class": "measurement",
        },
    ))

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_range/config",
        {
            "unique_id": f"ea_ev_{slug}_range",
            "name": "Range",
            "device": device,
            "state_topic": f"{base}/range",
            # Gated on its own topic: a car whose SoC source provides no
            # range shows "unavailable" instead of a stale or fake value.
            "availability": [
                {"topic": _STATUS_TOPIC},
                {"topic": f"{base}/range_available"},
            ],
            "availability_mode": "all",
            "unit_of_measurement": "km",
            "device_class": "distance",
            "state_class": "measurement",
            "icon": "mdi:map-marker-distance",
        },
    ))

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_charging_state/config",
        {
            "unique_id": f"ea_ev_{slug}_charging_state",
            "name": "Charging State",
            "device": device,
            "state_topic": f"{base}/charging_state",
            **bridge_avail,
            "device_class": "enum",
            "options": list(EV_DISPLAY_MODES),
            "icon": "mdi:ev-station",
        },
    ))

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_charging_mode/config",
        {
            "unique_id": f"ea_ev_{slug}_charging_mode",
            "name": "Charging Mode",
            "device": device,
            "state_topic": f"{base}/charging_mode",
            **bridge_avail,
            "device_class": "enum",
            "options": list(CHARGING_MODES),
            "icon": "mdi:ev-plug-type2",
        },
    ))

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_target_soc/config",
        {
            "unique_id": f"ea_ev_{slug}_target_soc",
            "name": "Target SoC",
            "device": device,
            "state_topic": f"{base}/target_soc",
            **bridge_avail,
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
            **bridge_avail,
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
            **bridge_avail,
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:pencil-circle",
        },
    ))

    configs.append((
        f"{_DISC}/binary_sensor/ea_ev_{slug}_plugged/config",
        {
            "unique_id": f"ea_ev_{slug}_plugged",
            "name": "Plugged In",
            "device": device,
            "state_topic": f"{base}/plugged",
            **data_avail,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "plug",
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
            **bridge_avail,
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
            **bridge_avail,
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
            **bridge_avail,
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
            **bridge_avail,
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:flash",
        },
    ))

    configs.extend(_weekly_discovery_configs(asset, device))
    return configs


def _price_discovery_configs() -> list[tuple[str, dict]]:
    """Current import price (with the curve as attributes) and effective price."""
    common = {
        "device": _SITE_DEVICE,
        "state_topic": _PRICE_TOPIC,
        "availability_topic": _STATUS_TOPIC,
        "unit_of_measurement": "EUR/kWh",
        "state_class": "measurement",
        "suggested_display_precision": 3,
        "icon": "mdi:cash-clock",
    }
    return [
        (
            f"{_DISC}/sensor/ea_electricity_price/config",
            {
                **common,
                "unique_id": "ea_electricity_price",
                "name": "Electricity price",
                "value_template": "{{ value_json.price }}",
                "json_attributes_topic": _PRICE_ATTR_TOPIC,
            },
        ),
        (
            f"{_DISC}/sensor/ea_effective_electricity_price/config",
            {
                **common,
                "unique_id": "ea_effective_electricity_price",
                "name": "Effective electricity price",
                "value_template": "{{ value_json.effective }}",
            },
        ),
    ]


def _savings_discovery_configs() -> list[tuple[str, dict]]:
    """Cumulative savings totals as monetary ``total`` sensors.

    ``total`` rather than ``total_increasing``: a single slot can lose money,
    and a decreasing value must not be read as a meter reset.
    """
    device = _SITE_DEVICE
    return [
        (
            f"{_DISC}/sensor/ea_{uid}/config",
            {
                "unique_id": f"ea_{uid}",
                "name": name,
                "device": device,
                "state_topic": _SAVINGS_TOPIC,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "availability_topic": _STATUS_TOPIC,
                "unit_of_measurement": "EUR",
                "device_class": "monetary",
                "state_class": "total",
                "suggested_display_precision": 2,
            },
        )
        for key, uid, name in _SAVINGS_SENSORS
    ]


def _weekly_discovery_configs(
    asset: EvChargingAsset, device: dict
) -> list[tuple[str, dict]]:
    """Number + time + switch per weekday, editing the weekly plan directly."""
    slug = asset.asset_id
    configs: list[tuple[str, dict]] = []
    for key, day in zip(_WEEKDAY_KEYS, _WEEKDAY_NAMES):
        topic = f"{_BASE}/{slug}/weekly/{key}"
        uid = f"ea_ev_{slug}_weekly_{key}"

        def common(field: str, name: str) -> dict:
            return {
                "unique_id": f"{uid}_{field}",
                "name": f"Weekly {day} {name}",
                "device": device,
                "state_topic": f"{topic}/{field}/state",
                "command_topic": f"{topic}/{field}/set",
                "availability_topic": _STATUS_TOPIC,
                "entity_category": "config",
            }

        configs.append((
            f"{_DISC}/number/{uid}_soc/config",
            {
                **common("soc", "Target SoC"),
                "unit_of_measurement": "%",
                "min": 0,
                "max": 100,
                "step": 5,
                "mode": "box",
                "icon": "mdi:battery-arrow-up-outline",
            },
        ))
        configs.append((
            f"{_DISC}/time/{uid}_time/config",
            {**common("time", "Departure"), "icon": "mdi:clock-end"},
        ))
        configs.append((
            f"{_DISC}/switch/{uid}_enabled/config",
            {
                **common("enabled", "Enabled"),
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:calendar-check",
            },
        ))
    return configs


def _weekly_state_messages(
    asset: EvChargingAsset, weekly: dict[int, EvWeeklyTarget]
) -> list[tuple[str, str, bool]]:
    """(topic, payload, retain) for all 7 weekdays; unset days show defaults."""
    msgs: list[tuple[str, str, bool]] = []
    for weekday, key in enumerate(_WEEKDAY_KEYS, start=1):
        row = default_weekly_target(asset, weekly, weekday)
        topic = f"{_BASE}/{asset.asset_id}/weekly/{key}"
        msgs.append((f"{topic}/soc/state", f"{row.target_soc_pct:.0f}", True))
        msgs.append((f"{topic}/time/state", f"{row.target_by}:00", True))
        msgs.append((f"{topic}/enabled/state", "ON" if row.enabled else "OFF", True))
    return msgs


def _parse_weekly_command(
    day_key: str, field: str, payload: str
) -> tuple[int, dict[str, Any]] | None:
    """Map a weekly ``…/{day}/{field}/set`` command to (ISO weekday, changes).

    Returns None for an unknown day/field or an out-of-range payload.
    """
    if day_key not in _WEEKDAY_KEYS:
        return None
    weekday = _WEEKDAY_KEYS.index(day_key) + 1
    try:
        if field == "soc":
            soc = float(payload)
            if not 0 <= soc <= 100:
                return None
            return weekday, {"target_soc_pct": soc}
        if field == "time":
            # HA time entity sends HH:MM:SS in local time
            t = time.fromisoformat(payload)
            return weekday, {"target_by": f"{t.hour:02d}:{t.minute:02d}"}
        if field == "enabled":
            return weekday, {"enabled": payload.strip().upper() in ("ON", "TRUE", "1")}
    except ValueError:
        return None
    return None


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
    """Return the plan-related (topic, payload, retain) messages for one asset.

    Live readings (SoC, range, plug state) are not part of this — they are
    published per control tick by ``publish_readings`` (see
    ``_reading_messages``).
    """
    slug = asset.asset_id
    base = f"{_BASE}/{slug}"
    msgs: list[tuple[str, str, bool]] = []

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


def _reading_messages(
    asset: EvChargingAsset, state: DeviceState | None
) -> list[tuple[str, str]]:
    """Return the live-reading (topic, payload) messages for one asset.

    All retained.  Topics without a value are left out (the broker keeps
    the last one, hidden behind the matching availability topic).
    """
    base = f"{_BASE}/{asset.asset_id}"
    # Data availability: we could read the chargepoint (have a SoC).  This is
    # intentionally independent of plug state — an unplugged car still has a
    # valid last-known SoC.
    data_ok = state is not None and state.soc_pct is not None
    msgs: list[tuple[str, str]] = [
        (f"{base}/available", "online" if data_ok else "offline"),
    ]
    if state is not None and state.soc_pct is not None:
        msgs.append((f"{base}/soc", f"{state.soc_pct:.0f}"))

    range_km = state.extra.get("range_km") if state is not None else None
    msgs.append((f"{base}/range_available", "online" if range_km is not None else "offline"))
    if range_km is not None:
        msgs.append((f"{base}/range", f"{range_km:.0f}"))

    plugged = state.extra.get("plugged") if state is not None else None
    if plugged is not None:
        msgs.append((f"{base}/plugged", "ON" if plugged else "OFF"))
    return msgs


def charging_mode_from_setpoint(setpoint_w: float | None, *, disabled: bool) -> str:
    """Map the EV contributor's commanded sentinel to a ``CHARGING_MODES`` value.

    The contributor returns ``None`` only when the chargepoint is disabled or
    the car is not connected (see ``EvChargerContributor.desired_setpoint_w``).
    """
    if disabled:
        return "disabled"
    if setpoint_w is None:
        return "disconnected"
    if setpoint_w > _MODE_THRESHOLD_W:
        return "instant_charging"
    if setpoint_w > 0:
        return "pv_charging"
    return "stop"

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

Device sub-devices
------------------
Every configured device whose readings do not already come from Home
Assistant (Modbus, Zendure, openWB MQTT, Tibber, differential, ioBroker …)
gets its own HA device, linked to the "Energy Assistant" device via
``via_device`` so it shows up as one of its sub-devices.  See
``select_discovery_devices`` for which devices qualify.  Entities are created
lazily: a sensor's discovery config is published the first time the device
reports a value for it (``power_w``, ``energy_kwh``, ``soc_pct`` and the
known ``extra`` keys in ``_DEVICE_FIELDS``), so a device only gets the
sensors it can actually fill.  Values are published per control tick, only
on change, under ``energy_assistant/device/{device_id}/…``; each device has
its own availability topic.

Savings KPIs
------------
With ``savings_kpis=True`` one more HA device, "Energy Assistant", carries
a cumulative monetary total (``state_class: total``): the real savings
against the simulated uncontrolled house.  Home Assistant's long-term statistics turn
those into any period (utility meter, statistics cards).  See
``core/savings.py`` for how the numbers are computed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiomqtt

from ..assets.ev import (
    EvChargingAsset,
    EvChargingGoal,
    EvWeeklyTarget,
    default_weekly_target,
)
from ..core.config import MqttConfig
from ..core.device_model import DeviceCategory, DeviceSpec
from ..core.models import EV_DISPLAY_MODES, Behaviour, DeviceState
from ..core.price_curve import PriceSlot, current_slot, price_curve_payload

_log = logging.getLogger(__name__)

_BASE = "energy_assistant/vehicle"
# Above this a charger counts as charging (below: standby / measurement noise).
_CHARGING_W = 100.0
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
)
# Sensors of the previous (predicted vs. real) accounting: their discovery
# configs are cleared so Home Assistant removes the entities.
_REMOVED_SAVINGS_UIDS = ("savings_predicted", "savings_deviation", "savings_predicted_last_plan")
_DEVICE_BASE = "energy_assistant/device"
# Device types whose readings come from Home Assistant in the first place
# (re-publishing them would only duplicate HA's own entities), and virtual
# devices with no readings of their own.
_HA_SOURCED_MARKER = "homeassistant"
_VIRTUAL_DEVICE_TYPES = frozenset({"generic_consumer"})


@dataclass(frozen=True)
class DiscoveryDevice:
    """A configured device exposed to Home Assistant as a sub-device."""

    device_id: str
    name: str
    category: DeviceCategory
    model: str


@dataclass(frozen=True)
class _Field:
    """One publishable reading: DeviceState attribute or ``extra`` key."""

    key: str
    name: str
    fmt: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    entity_category: str | None = None
    icon: str | None = None


# (source, field) — source is "attr" for DeviceState attributes, "extra" for
# DeviceState.extra keys.  Unknown extra keys are not published.
_DEVICE_FIELDS: tuple[tuple[str, _Field], ...] = (
    ("attr", _Field("power_w", "Power", ".0f", "W", "power")),
    ("attr", _Field("energy_kwh", "Energy", ".3f", "kWh", "energy", "total_increasing")),
    ("attr", _Field("soc_pct", "State of Charge", ".1f", "%", "battery")),
    ("extra", _Field("import_w", "Import power", ".0f", "W", "power")),
    ("extra", _Field("export_w", "Export power", ".0f", "W", "power")),
    ("extra", _Field("charge_w", "Charge power", ".0f", "W", "power")),
    ("extra", _Field("discharge_w", "Discharge power", ".0f", "W", "power")),
    ("extra", _Field("home_w", "Home output power", ".0f", "W", "power")),
    ("extra", _Field("grid_input_w", "Grid input power", ".0f", "W", "power")),
    ("extra", _Field("solar_w", "Solar input power", ".0f", "W", "power")),
    ("extra", _Field("meter_production_kwh", "Exported energy", ".3f", "kWh", "energy",
                     "total_increasing")),
    ("extra", _Field("accumulated_consumption_today_kwh", "Consumption today", ".3f", "kWh",
                     "energy", "total_increasing")),
    ("extra", _Field("frequency_hz", "Grid frequency", ".2f", "Hz", "frequency")),
    ("extra", _Field("max_cha_w", "Max charge power", ".0f", "W", "power",
                     entity_category="diagnostic")),
    ("extra", _Field("max_dsch_w", "Max discharge power", ".0f", "W", "power",
                     entity_category="diagnostic")),
    ("extra", _Field("input_limit_w", "Input limit", ".0f", "W", "power",
                     entity_category="diagnostic")),
    ("extra", _Field("output_limit_w", "Output limit", ".0f", "W", "power",
                     entity_category="diagnostic")),
    ("extra", _Field("min_soc_pct", "Min SoC", ".0f", "%", None,
                     entity_category="diagnostic", icon="mdi:battery-arrow-down")),
    ("extra", _Field("max_soc_pct", "Max SoC", ".0f", "%", None,
                     entity_category="diagnostic", icon="mdi:battery-arrow-up")),
    ("extra", _Field("pulse_battery_v", "Pulse battery voltage", ".2f", "V", "voltage",
                     entity_category="diagnostic")),
)
# Readings already shown on the EV device (the car's SoC) are not repeated
# on the chargepoint.
_CATEGORY_SKIP_FIELDS: dict[DeviceCategory, frozenset[str]] = {
    DeviceCategory.CHARGER: frozenset({"soc_pct"}),
}

_RECONNECT_DELAY_S = 30
CHARGING_MODES = ("instant_charging", "pv_charging", "stop", "disconnected", "disabled")
# Sentinel boundary between PV and Instant mode — see assets/ev.py.
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
        devices: list[DiscoveryDevice] | None = None,
        remove_stale_vehicles: bool = False,
        chargers: list[tuple[str, str]] | None = None,
        on_select_charger: Callable[[str | None, str | None], Awaitable[None]] | None = None,
        on_set_guest_mode: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        # Remove vehicle entities from Home Assistant that this configuration
        # no longer has (see _stale_vehicle_topics).
        self._remove_stale_vehicles = remove_stale_vehicles
        # (charger id, label) of every charger, for the charger selects.
        self._chargers = list(chargers or [])
        self._on_select_charger = on_select_charger
        self._on_set_guest_mode = on_set_guest_mode
        self._savings_kpis = savings_kpis
        self._devices = list(devices or [])
        # Discovery configs of device sensors seen so far (topic → payload)
        # — created lazily as readings appear, re-published on reconnect.
        self._device_configs: dict[str, str] = {}
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
            state = device_states.get(asset.asset_id)
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
            label = next((lbl for cid, lbl in self._chargers if cid == asset.device_id), None)
            vehicle = device_states.get(asset.asset_id)
            await self._queue_if_changed(_reading_messages(
                asset, vehicle,
                device_states.get(asset.device_id) if asset.device_id else None,
                label,
            ))
            if self._chargers:
                status = vehicle.extra.get("charger_status") if vehicle is not None else None
                await self._queue_if_changed([(
                    f"{_BASE}/{asset.asset_id}/charger_select",
                    _charger_select_state(status, self._chargers),
                )])
        if self._chargers:
            here = next(((lbl, st) for cid, lbl in self._chargers
                         if (st := device_states.get(cid)) is not None and st.extra.get("guest_here")),
                        None)
            mode = here[1].extra.get("guest_mode") if here else None
            mode_option = next((o for o, m in GUEST_MODE_OPTIONS.items() if m == mode), "PV")
            await self._queue_if_changed([
                (f"{_GUEST_BASE}/charger_select", here[0] if here else NO_CHARGER),
                (f"{_GUEST_BASE}/mode", mode_option),
            ])
        for device in self._devices:
            state = device_states.get(device.device_id)
            for field in _device_fields_present(device, state):
                topic, config = _device_sensor_config(device, field)
                if topic not in self._device_configs:
                    payload = json.dumps(config)
                    self._device_configs[topic] = payload
                    await self._queue.put((topic, payload, True))
            await self._queue_if_changed(_device_reading_messages(device, state))

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
                await client.subscribe(f"{_BASE}/{slug}/charger_select/set")
            if self._chargers:
                await client.subscribe(f"{_GUEST_BASE}/charger_select/set")
                await client.subscribe(f"{_GUEST_BASE}/mode/set")
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
        # energy_assistant/guest/charger_select/set
        if topic == f"{_GUEST_BASE}/charger_select/set":
            await self._select_charger(None, payload)
            return
        if topic == f"{_GUEST_BASE}/mode/set":
            mode = GUEST_MODE_OPTIONS.get(payload)
            if mode is None or self._on_set_guest_mode is None:
                _log.info("MQTT guest mode: %r is not an option", payload)
                return
            try:
                await self._on_set_guest_mode(mode)
            except ValueError as exc:
                _log.warning("MQTT guest mode: %s", exc)
            self._live_states.pop(f"{_GUEST_BASE}/mode", None)
            return
        # energy_assistant/vehicle/{vehicle_id}/{cmd}/set
        parts = topic.split("/")
        if len(parts) < 5:
            return
        asset_id = parts[2]
        cmd = parts[3]
        asset = next((a for a in self._assets if a.asset_id == asset_id), None)
        if asset is None:
            _log.warning("MQTT command for unknown asset: %r", asset_id)
            return

        if cmd == "charger_select":
            await self._select_charger(asset_id, payload)
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

    async def _select_charger(self, vehicle_id: str | None, option: str) -> None:
        """A car's (``None``: the guest's) charger select was set."""
        if self._on_select_charger is None:
            return
        charger_id = _charger_of_option(option, self._chargers)
        who = vehicle_id or "guest"
        if charger_id is False:
            _log.info("MQTT charger select %s: %r is not a charger", who, option)
            return
        _log.info("MQTT charger select: %s → %s", who, charger_id or "none")
        try:
            await self._on_select_charger(vehicle_id, charger_id)
        except ValueError as exc:
            _log.warning("MQTT charger select %s: %s", who, exc)
        # Re-publish what it really is on the next tick.
        topic = f"{_BASE}/{vehicle_id}/charger_select" if vehicle_id else f"{_GUEST_BASE}/charger_select"
        self._live_states.pop(topic, None)

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
        vehicle_configs: list[tuple[str, dict]] = []
        for asset in self._assets:
            device = {
                "identifiers": [f"energy_assistant_ev_{asset.asset_id}"],
                "name": asset.label,
                "manufacturer": "Energy Assistant",
                "model": "Vehicle",
            }
            vehicle_configs.extend(_discovery_configs(asset, device, chargers=self._chargers))
        if self._remove_stale_vehicles:
            # First remove what is no longer configured, so that a vehicle that
            # comes back under another id finds its entity ids free.
            stale = await _stale_vehicle_topics(client, {topic for topic, _ in vehicle_configs})
            for topic in sorted(stale):
                await client.publish(topic, "", retain=True)
            if stale:
                _log.info("MQTT discovery: removed %d entities of vehicles that are no longer "
                          "configured", len(stale))
                await asyncio.sleep(1.0)  # let Home Assistant drop them before the new ones
        for config_topic, config_payload in vehicle_configs:
            await client.publish(config_topic, json.dumps(config_payload), retain=True)
        if self._chargers:
            for config_topic, config_payload in (_guest_select_config(self._chargers),
                                                 _guest_mode_select_config()):
                await client.publish(config_topic, json.dumps(config_payload), retain=True)
        for config_topic, config_payload in _price_discovery_configs():
            await client.publish(config_topic, json.dumps(config_payload), retain=True)
        if self._savings_kpis:
            for config_topic, config_payload in _savings_discovery_configs():
                await client.publish(config_topic, json.dumps(config_payload), retain=True)
            for uid in _REMOVED_SAVINGS_UIDS:
                await client.publish(f"{_DISC}/sensor/ea_{uid}/config", "", retain=True)
        for config_topic, payload in list(self._device_configs.items()):
            await client.publish(config_topic, payload, retain=True)
        _log.info(
            "MQTT Discovery published for %d EV asset(s), %d device(s)",
            len(self._assets), len(self._devices),
        )


# ------------------------------------------------------------------
# Pure helpers — Discovery config generation
# ------------------------------------------------------------------

#: Prefix of the discovery object ids of vehicle entities.
_VEHICLE_OBJECT_PREFIX = "ea_ev_"


async def _stale_vehicle_topics(
    client: aiomqtt.Client, current: set[str], quiet_s: float = 1.0
) -> set[str]:
    """Retained discovery topics of vehicle entities that are not in *current*.

    They belong to vehicles that were removed from the configuration (or
    renamed); clearing them makes Home Assistant remove the entities.  Reads
    the broker's retained discovery messages until none arrives for *quiet_s*.
    """
    pattern = f"{_DISC}/+/+/config"
    stale: set[str] = set()
    await client.subscribe(pattern)
    try:
        messages = aiter(client.messages)
        while True:
            try:
                message = await asyncio.wait_for(anext(messages), timeout=quiet_s)
            except (TimeoutError, StopAsyncIteration):
                break
            topic = str(message.topic)
            parts = topic.split("/")
            if (len(parts) == 4 and parts[2].startswith(_VEHICLE_OBJECT_PREFIX)
                    and message.retain and message.payload and topic not in current):
                stale.add(topic)
    finally:
        await client.unsubscribe(pattern)
    return stale


def _discovery_configs(
    asset: EvChargingAsset, device: dict, uid: str | None = None,
    chargers: list[tuple[str, str]] | None = None,
) -> list[tuple[str, dict]]:
    """Discovery configs of one vehicle.  *uid* is the id used in unique ids
    and config topics (default: the vehicle id)."""
    slug = uid or asset.asset_id
    base = f"{_BASE}/{asset.asset_id}"
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

    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_charging_power/config",
        {
            "unique_id": f"ea_ev_{slug}_charging_power",
            "name": "Charging Power",
            "device": device,
            "state_topic": f"{base}/charging_power",
            **bridge_avail,
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "suggested_display_precision": 1,
        },
    ))
    configs.append((
        f"{_DISC}/binary_sensor/ea_ev_{slug}_charging_now/config",
        {
            "unique_id": f"ea_ev_{slug}_charging_now",
            "name": "Charging Now",
            "device": device,
            "state_topic": f"{base}/charging_now",
            **bridge_avail,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "battery_charging",
        },
    ))
    configs.append((
        f"{_DISC}/sensor/ea_ev_{slug}_location/config",
        {
            "unique_id": f"ea_ev_{slug}_location",
            "name": "Location",
            "device": device,
            "state_topic": f"{base}/location",
            **bridge_avail,
            "icon": "mdi:map-marker-radius",
        },
    ))
    if chargers:
        configs.append((
            f"{_DISC}/select/ea_ev_{slug}_charger/config",
            {
                "unique_id": f"ea_ev_{slug}_charger",
                "name": "Charger",
                "device": device,
                "state_topic": f"{base}/charger_select",
                "command_topic": f"{base}/charger_select/set",
                "options": _charger_options(chargers),
                **bridge_avail,
                "icon": "mdi:ev-station",
            },
        ))

    configs.extend(_weekly_discovery_configs(asset, device, slug))
    return configs


#: Option of a charger select: no charger.
NO_CHARGER = "No charger"   # not "None": HA treats that select payload as unknown
_GUEST_BASE = "energy_assistant/guest"


def _charger_options(chargers: list[tuple[str, str]]) -> list[str]:
    return [label for _cid, label in chargers] + [NO_CHARGER]


def _charger_select_state(status: dict | None, chargers: list[tuple[str, str]]) -> str:
    """The select's option for a car's charger status (``SessionManager.status``):
    the charger it is at, selected for or defaults to; ``None`` when it has none
    or its default charger is taken by another car."""
    if not status or status.get("state") not in ("plugged", "selected", "default"):
        return NO_CHARGER
    return next((label for cid, label in chargers if cid == status.get("charger_id")), NO_CHARGER)


def _charger_of_option(option: str, chargers: list[tuple[str, str]]) -> str | None | bool:
    """Charger id of *option*, ``None`` for no charger, ``False`` if unknown."""
    if option == NO_CHARGER:
        return None
    return next((cid for cid, label in chargers if label == option), False)


#: Options of the guest car's mode select → the app's guest mode.
GUEST_MODE_OPTIONS = {"PV": "pv_only", "Charge now": "instant"}


def _guest_mode_select_config() -> tuple[str, dict]:
    """Select on the site device: how the guest car charges."""
    return (f"{_DISC}/select/ea_guest_mode/config", {
        "unique_id": "ea_guest_mode",
        "name": "Guest car mode",
        "device": _SITE_DEVICE,
        "state_topic": f"{_GUEST_BASE}/mode",
        "command_topic": f"{_GUEST_BASE}/mode/set",
        "options": list(GUEST_MODE_OPTIONS),
        "availability_topic": _STATUS_TOPIC,
        "icon": "mdi:solar-power-variant",
    })


def _guest_select_config(chargers: list[tuple[str, str]]) -> tuple[str, dict]:
    """Select on the site device: the charger a guest car charges at (or will)."""
    return (f"{_DISC}/select/ea_guest_charger/config", {
        "unique_id": "ea_guest_charger",
        "name": "Guest car charger",
        "device": _SITE_DEVICE,
        "state_topic": f"{_GUEST_BASE}/charger_select",
        "command_topic": f"{_GUEST_BASE}/charger_select/set",
        "options": [NO_CHARGER] + [label for _cid, label in chargers],
        "availability_topic": _STATUS_TOPIC,
        "icon": "mdi:account-clock",
    })


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
    asset: EvChargingAsset, device: dict, uid: str | None = None
) -> list[tuple[str, dict]]:
    """Number + time + switch per weekday, editing the weekly plan directly."""
    slug = uid or asset.asset_id
    configs: list[tuple[str, dict]] = []
    for key, day in zip(_WEEKDAY_KEYS, _WEEKDAY_NAMES):
        topic = f"{_BASE}/{asset.asset_id}/weekly/{key}"
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
    asset: EvChargingAsset,
    state: DeviceState | None,
    charger: DeviceState | None = None,
    charger_label: str | None = None,
) -> list[tuple[str, str]]:
    """Return the live-reading (topic, payload) messages for one vehicle.

    *state* is the vehicle's (SoC with the estimate, range); *charger* the
    state of the charger it is at, if any (plug state).  All retained.
    Topics without a value are left out (the broker keeps the last one,
    hidden behind the matching availability topic).
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

    # Plugged in = at one of our chargers with the cable in; away = not.
    plugged = charger.extra.get("plugged") if charger is not None else False
    if plugged is not None:
        msgs.append((f"{base}/plugged", "ON" if plugged else "OFF"))
    # Where the car is and what it draws there (0 / OFF when away), so a
    # dashboard tile follows the car to whichever charger it is at.
    msgs.append((f"{base}/location", (charger_label or asset.device_id) if asset.device_id else "away"))
    power_w = (charger.power_w or 0.0) if charger is not None else 0.0
    msgs.append((f"{base}/charging_power", f"{max(power_w, 0.0) / 1000:.2f}"))
    msgs.append((f"{base}/charging_now", "ON" if power_w > _CHARGING_W else "OFF"))
    return msgs


# ------------------------------------------------------------------
# Pure helpers — device sub-devices
# ------------------------------------------------------------------

def select_discovery_devices(
    devices: Mapping[str, DeviceSpec],
    built: Collection[str],
) -> list[DiscoveryDevice]:
    """Pick the configured devices to expose as HA sub-devices.

    *built* are the ids of the devices that were actually built.
    Skipped: devices fed from Home Assistant (plugin type containing
    ``homeassistant``), virtual devices without readings, devices that
    failed to build, and devices with ``ha_discovery: false``.  The HA
    device name is the device's ``label:`` (or its id, title-cased).
    """
    out: list[DiscoveryDevice] = []
    for device_id, spec in devices.items():
        type_name = spec.type
        if (
            device_id not in built
            or _HA_SOURCED_MARKER in type_name
            or type_name in _VIRTUAL_DEVICE_TYPES
            or not spec.ha_discovery
        ):
            continue
        out.append(DiscoveryDevice(
            device_id=device_id,
            name=spec.display_name,
            category=spec.category,
            model=type_name or "device",
        ))
    return out


def _device_value(source: str, field: _Field, state: DeviceState) -> float | None:
    raw = getattr(state, field.key, None) if source == "attr" else state.extra.get(field.key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _device_fields_present(
    device: DiscoveryDevice, state: DeviceState | None
) -> list[_Field]:
    """The fields *state* has a numeric value for (in table order)."""
    if state is None:
        return []
    skip = _CATEGORY_SKIP_FIELDS.get(device.category, frozenset())
    return [
        field for source, field in _DEVICE_FIELDS
        if field.key not in skip and _device_value(source, field, state) is not None
    ]


def _device_sensor_config(device: DiscoveryDevice, field: _Field) -> tuple[str, dict]:
    """Discovery (topic, config) for one sensor of a sub-device."""
    uid = f"ea_dev_{device.device_id}_{field.key}"
    base = f"{_DEVICE_BASE}/{device.device_id}"
    config: dict[str, Any] = {
        "unique_id": uid,
        "name": field.name,
        "device": {
            "identifiers": [f"energy_assistant_device_{device.device_id}"],
            "name": device.name,
            "manufacturer": "Energy Assistant",
            "model": device.model,
            "via_device": _SITE_DEVICE["identifiers"][0],
        },
        "state_topic": f"{base}/{field.key}",
        "availability": [
            {"topic": _STATUS_TOPIC},
            {"topic": f"{base}/available"},
        ],
        "availability_mode": "all",
    }
    for key in ("unit", "device_class", "state_class", "entity_category", "icon"):
        value = getattr(field, key)
        if value is not None:
            config["unit_of_measurement" if key == "unit" else key] = value
    return f"{_DISC}/sensor/{uid}/config", config


def _device_reading_messages(
    device: DiscoveryDevice, state: DeviceState | None
) -> list[tuple[str, str]]:
    """Live (topic, payload) messages for one sub-device.

    The device counts as available while it reports anything at all — the
    plugins' ``available`` flag can mean more than reachability (an openWB
    chargepoint is "unavailable" whenever no car is plugged in, yet its
    power reading is valid).
    """
    base = f"{_DEVICE_BASE}/{device.device_id}"
    present = _device_fields_present(device, state)
    online = state is not None and (state.available or bool(present))
    msgs: list[tuple[str, str]] = [(f"{base}/available", "online" if online else "offline")]
    if state is None:
        return msgs
    by_key = {field.key: source for source, field in _DEVICE_FIELDS}
    for field in present:
        value = _device_value(by_key[field.key], field, state)
        msgs.append((f"{base}/{field.key}", format(value, field.fmt)))
    return msgs


def charging_mode_from_behaviour(behaviour: Behaviour | None, *, disabled: bool) -> str:
    """Map the EV contributor's commanded behaviour to a ``CHARGING_MODES`` value.

    No behaviour means no command: the chargepoint is disabled or the car is
    not connected (see ``EvChargerContributor.desired_setpoint_w``).
    """
    if disabled:
        return "disabled"
    if behaviour is None:
        return "disconnected"
    if behaviour is Behaviour.CHARGE:
        return "instant_charging"
    if behaviour is Behaviour.ABSORB_SURPLUS:
        return "pv_charging"
    return "stop"
"""Application — main orchestrator for the Energy Assistant platform.

Wires all platform components together and runs three concurrent async loops:

Polling loop (``poll_interval_s``, default 30 s)
    Reads current state from every registered device, persists it to the
    SQLite history store, and publishes ``DeviceStateEvent`` on the bus.
    After the very first tick it initialises the ``BatteryCostLedger`` from
    live SoC readings.

Planning loop (``plan_interval_s``, default 3600 s)
    Assembles an ``OptimizationContext`` from current device states, all
    forecast providers, and the tariff schedule, then runs the MILP
    optimizer and publishes a ``PlanUpdatedEvent``.  The ``ControlLoop``
    subscribes to this event and replaces its active plan immediately.

Control loop (``control_interval_s``, default 30 s)
    Builds a ``LiveSituation`` snapshot (grid power, current spot price,
    PV opportunity price, device states, elapsed dt) and calls
    ``ControlLoop.tick()``.  Each registered ``ControlContributor`` decides
    its desired setpoint; the loop sends commands and updates the ledger.

Usage (CLI)::

    python -m energy_assistant               # uses ./config.yaml
    python -m energy_assistant path/to/config.yaml

Usage (programmatic)::

    app = Application("config.yaml")
    await app.run_forever()          # blocks until SIGINT / SIGTERM
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections import defaultdict
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from ..assets.ev import EvChargerContributor, EvChargingAsset, EvChargingGoal
from ..assets.loader import parse_ev_assets, parse_threshold_assets, resolve_active_goals
from ..assets.threshold import ThresholdControlContributor
from ..config.yaml import YamlConfigLoader
from ..core.config import AppConfig
from ..core.control import ControlLoop, LiveSituation, StorageControlContributor
from ..core.event import DeviceStateEvent, EventBus, PlanUpdatedEvent
from ..core.forecast import ForecastProvider
from ..core.ledger import BatteryCostLedger
from ..core.models import (
    DeviceRole,
    ForecastPoint,
    ForecastQuantity,
    Measurement,
    StorageConstraints,
    TariffPoint,
    ThresholdConstraints,
    intent_display_mode,
)
from ..core.optimizer import OptimizationContext
from ..core.plugin_registry import BuildContext
from ..core.registry import DeviceRegistry
from ..core.tariff import TariffModel
from ..core.topology import TopologyNode
from ..loader.device_loader import build, build_all_forecasts, make_build_context
from ..plugins import registry as plugin_registry
from ..plugins.milp_highs import MilpHigsOptimizer
from ..plugins.mqtt_publisher import EvMqttPublisher
from ..storage.sqlite import SqliteStorageBackend

_log = logging.getLogger(__name__)


@dataclasses.dataclass
class _TariffZone:
    """Devices assigned to a single tariff zone for market-price computation."""

    tariff_id: str
    meter_ids: list[str] = dataclasses.field(default_factory=list)
    """``role=meter`` devices that measure grid import for this circuit."""
    producer_ids: list[str] = dataclasses.field(default_factory=list)
    """``role=producer`` (PV) devices physically on this circuit."""
    storage_ids: list[str] = dataclasses.field(default_factory=list)
    """``role=storage`` devices (batteries) physically on this circuit."""
    diff_minuend_id: str | None = None
    """For differential zones (e.g. heatpump = Z1 − Z2): the Z1 (minuend) device."""
    diff_subtrahend_id: str | None = None
    """For differential zones: the Z2 (subtrahend) device whose zone's market price
    is used for the feedback contribution when Z2 is exporting."""



# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _storage_constraints_from_config(cfg: AppConfig) -> list[StorageConstraints]:
    """Extract ``StorageConstraints`` for every device with ``role: storage``."""
    result: list[StorageConstraints] = []
    for device_id, dcfg in cfg.devices.items():
        if dcfg.get("role") != "storage":
            continue
        try:
            purchase_price = dcfg.get("purchase_price_eur")
            cycle_life = dcfg.get("cycle_life") or dcfg.get("cycle_lifetime")
            result.append(
                StorageConstraints(
                    device_id=device_id,
                    capacity_kwh=float(dcfg.get("capacity_kwh", 0.0)),
                    max_charge_kw=float(dcfg.get("max_charge_kw", 0.0)),
                    max_discharge_kw=float(dcfg.get("max_discharge_kw", 0.0)),
                    charge_efficiency=float(dcfg.get("charge_efficiency", 0.95)),
                    discharge_efficiency=float(dcfg.get("discharge_efficiency", 0.95)),
                    min_soc_pct=float(dcfg.get("min_soc_pct", 0.0)),
                    max_soc_pct=float(dcfg.get("max_soc_pct", 100.0)),
                    purchase_price_eur=float(purchase_price) if purchase_price is not None else None,
                    cycle_life=int(cycle_life) if cycle_life is not None else None,
                    no_grid_charge=bool(dcfg.get("no_grid_charge", False)),
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Could not build StorageConstraints for %r: %s", device_id, exc)
    return result


async def _current_export_price(tariffs: dict[str, TariffModel]) -> float:
    """Return the current feed-in (export) price from the first tariff that has one."""
    for tariff in tariffs.values():
        try:
            sched = await tariff.export_price_schedule(timedelta(hours=1))
            if sched and any(tp.price_eur_per_kwh > 0.001 for tp in sched):
                return sched[0].price_eur_per_kwh
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def _infer_effective_horizon(
    forecasts: dict[ForecastQuantity, list[ForecastPoint]],
    step_minutes: int,
    cap: timedelta,
) -> timedelta:
    """Cap the planning horizon at the shortest available live data forecast.

    Only PRICE and PV_GENERATION forecasts are used as limits — CONSUMPTION
    is typically a static profile that extends indefinitely, so including it
    would not reflect actual data availability.

    The horizon is clipped to the **minimum** last-timestamp across all
    available data sources.  If Tibber only has today's prices (e.g. tomorrow's
    not yet published), the optimizer stops there rather than running into a
    zero-padded tail that makes grid electricity appear free.
    """
    now = datetime.now(timezone.utc)
    limits: list[datetime] = []
    for quantity in (ForecastQuantity.PRICE, ForecastQuantity.PV_GENERATION):
        pts = forecasts.get(quantity, [])
        if pts:
            limits.append(max(p.timestamp for p in pts))
    latest = min(limits) if limits else now + cap
    raw_delta = latest - now
    capped = min(raw_delta, cap)
    step_td = timedelta(minutes=step_minutes)
    n_steps = max(1, int(capped.total_seconds() / step_td.total_seconds()))
    return step_td * n_steps


def _extend_forecast_points_with_daily_repeat(
    points: list[ForecastPoint],
    horizon: timedelta,
) -> tuple[list[ForecastPoint], set[datetime]]:
    """Extend a forecast by repeating its latest day profile.

    Extension is capped to the smaller of:
    - one native prediction span (derived from source data), and
    - 48 hours.
    """
    if not points:
        return [], set()

    pts = sorted(points, key=lambda p: p.timestamp)
    if len(pts) < 2:
        return pts, set()

    diffs = [
        b.timestamp - a.timestamp
        for a, b in zip(pts, pts[1:])
        if b.timestamp > a.timestamp
    ]
    if not diffs:
        return pts, set()
    step = min(diffs)
    if step.total_seconds() <= 0:
        return pts, set()

    native_span = max(step, (pts[-1].timestamp - pts[0].timestamp) + step)
    extension_cap = min(native_span, timedelta(hours=48))
    if extension_cap <= timedelta(0):
        return pts, set()

    now = datetime.now(timezone.utc)
    target_end = min(now + horizon, pts[-1].timestamp + extension_cap)
    if pts[-1].timestamp >= target_end:
        return pts, set()

    tz = pts[-1].timestamp.tzinfo or timezone.utc
    by_day: dict[date, list[ForecastPoint]] = defaultdict(list)
    for p in pts:
        by_day[p.timestamp.astimezone(tz).date()].append(p)
    template_day = max(by_day.items(), key=lambda kv: (len(kv[1]), kv[0]))[0]
    template = by_day.get(template_day, [])
    template = sorted(template, key=lambda p: p.timestamp)
    if not template:
        return pts, set()

    def _tod_seconds(ts: datetime) -> int:
        lt = ts.astimezone(tz)
        return lt.hour * 3600 + lt.minute * 60 + lt.second

    template_by_tod = sorted(
        [(_tod_seconds(p.timestamp), float(p.value)) for p in template],
        key=lambda x: x[0],
    )
    first_sec = template_by_tod[0][0]
    last_sec = template_by_tod[-1][0]

    def _pv_value_for_tod(sec_of_day: int) -> float:
        # PV should be 0 outside the observed daylight window.
        if sec_of_day < first_sec or sec_of_day > last_sec:
            return 0.0
        prev_val = 0.0
        for sec, val in template_by_tod:
            if sec > sec_of_day:
                break
            prev_val = val
        return prev_val

    out = list(pts)
    estimated_ts: set[datetime] = set()
    cursor = pts[-1].timestamp + step
    while cursor <= target_end:
        sec = _tod_seconds(cursor)
        out.append(ForecastPoint(timestamp=cursor, value=_pv_value_for_tod(sec)))
        estimated_ts.add(cursor)
        cursor += step

    return out, estimated_ts


def _extend_tariff_schedule_with_daily_repeat(
    schedule: list[TariffPoint],
    horizon: timedelta,
) -> tuple[list[TariffPoint], set[datetime]]:
    """Extend a tariff schedule by repeating its latest day profile.

    Extension is capped to the smaller of:
    - one native prediction span (derived from source data), and
    - 48 hours.
    """
    if not schedule:
        return [], set()

    pts = sorted(schedule, key=lambda p: p.timestamp)
    if len(pts) < 2:
        return pts, set()

    diffs = [
        b.timestamp - a.timestamp
        for a, b in zip(pts, pts[1:])
        if b.timestamp > a.timestamp
    ]
    if not diffs:
        return pts, set()
    step = min(diffs)
    if step.total_seconds() <= 0:
        return pts, set()

    native_span = max(step, (pts[-1].timestamp - pts[0].timestamp) + step)
    extension_cap = min(native_span, timedelta(hours=48))
    if extension_cap <= timedelta(0):
        return pts, set()

    now = datetime.now(timezone.utc)
    target_end = min(now + horizon, pts[-1].timestamp + extension_cap)
    if pts[-1].timestamp >= target_end:
        return pts, set()

    tz = pts[-1].timestamp.tzinfo or timezone.utc
    by_day: dict[date, list[TariffPoint]] = defaultdict(list)
    for p in pts:
        by_day[p.timestamp.astimezone(tz).date()].append(p)
    template_day = max(by_day.items(), key=lambda kv: (len(kv[1]), kv[0]))[0]
    template = by_day.get(template_day, [])
    template = sorted(template, key=lambda p: p.timestamp)
    if not template:
        return pts, set()

    def _tod_seconds(ts: datetime) -> int:
        lt = ts.astimezone(tz)
        return lt.hour * 3600 + lt.minute * 60 + lt.second

    template_by_tod = sorted(
        [(_tod_seconds(p.timestamp), float(p.price_eur_per_kwh)) for p in template],
        key=lambda x: x[0],
    )

    def _nearest_value_for_tod(sec_of_day: int) -> float:
        best_sec, best_val = min(
            template_by_tod,
            key=lambda it: min(abs(it[0] - sec_of_day), 86400 - abs(it[0] - sec_of_day)),
        )
        _ = best_sec
        return best_val

    out = list(pts)
    estimated_ts: set[datetime] = set()
    cursor = pts[-1].timestamp + step
    while cursor <= target_end:
        sec = _tod_seconds(cursor)
        out.append(TariffPoint(timestamp=cursor, price_eur_per_kwh=_nearest_value_for_tod(sec)))
        estimated_ts.add(cursor)
        cursor += step

    return out, estimated_ts


async def _collect_forecasts(
    providers: list[ForecastProvider],
    horizon: timedelta,
) -> dict[ForecastQuantity, list[ForecastPoint]]:
    """Call every provider and group points by quantity.

    Multiple providers for the same quantity (e.g. several consumption
    profiles for different devices) have their values summed per timestamp
    so the result has exactly one point per timestamp per quantity.
    """
    from collections import defaultdict

    buckets: dict[ForecastQuantity, dict[datetime, float]] = {}
    for provider in providers:
        try:
            pts = await provider.get_forecast(horizon)
            q = provider.quantity
            if q not in buckets:
                buckets[q] = defaultdict(float)
            for pt in pts:
                buckets[q][pt.timestamp] += float(pt.value)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Forecast provider %r failed: %s", getattr(provider, "quantity", "?"), exc)

    return {
        q: sorted(
            [ForecastPoint(timestamp=ts, value=v) for ts, v in by_ts.items()],
            key=lambda p: p.timestamp,
        )
        for q, by_ts in buckets.items()
    }


async def _virtual_forecast_power_w(device_cfg: dict) -> float | None:
    """Return forecast power in watts for virtual generic consumers."""
    if device_cfg.get("type") != "generic_consumer":
        return None
    forecast_cfg = device_cfg.get("forecast")
    if not isinstance(forecast_cfg, dict):
        return None
    if forecast_cfg.get("type") != "static_profile":
        return None

    try:
        from ..plugins.static_profile.forecast import StaticProfileForecast

        provider = StaticProfileForecast(profile=forecast_cfg.get("profile", {}))
        pts = await provider.get_forecast(timedelta(hours=1))
        if not pts:
            return None
        # First point is the current hour bucket.
        return float(pts[0].value) * 1000.0
    except Exception as exc:  # noqa: BLE001
        _log.debug("Could not compute virtual forecast power: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Application
# ──────────────────────────────────────────────────────────────────────────────


class Application:
    """Main orchestrator — wires and runs all platform loops.

    Typical usage via ``run_forever()``::

        app = Application("config.yaml")
        asyncio.run(app.run_forever())

    Or manage the lifecycle yourself::

        await app.start()
        try:
            await asyncio.gather(*app.tasks)
        finally:
            await app.stop()
    """

    def __init__(
        self,
        config_path: Path | str = "config.yaml",
        db_path: Path | str = "data/history.db",
    ) -> None:
        self._config_path = Path(config_path)
        self._db_path = Path(db_path)
        self.tasks: list[asyncio.Task[None]] = []

        # Set by start()
        self._cfg: AppConfig
        self._build_ctx: BuildContext
        self._registry: DeviceRegistry
        self._tariffs: dict[str, TariffModel]
        self._topology: TopologyNode | None
        self._storage: SqliteStorageBackend
        self._bus: EventBus
        self._ledger: BatteryCostLedger
        self._control_loop: ControlLoop
        self._optimizer: MilpHigsOptimizer
        self._forecast_providers: list[ForecastProvider]
        self._storage_constraints: list[StorageConstraints]
        self._default_tariff: TariffModel | None
        self._grid_meter_id: str | None
        self._pv_opportunity_price: float
        self._horizon: timedelta
        self._last_forecast_pts: dict[ForecastQuantity, list[ForecastPoint]] = {}
        self._last_price_estimated_by_ts: dict[datetime, bool] = {}
        self._last_variable_price_pts: list[ForecastPoint] = []
        self._last_variable_price_estimated_by_ts: dict[datetime, bool] = {}
        self._last_pv_estimated_by_ts: dict[datetime, bool] = {}
        self._plan_interval_s: float
        self._poll_interval_s: float
        self._control_interval_s: float
        self._dry_run: bool
        self._first_poll_done: asyncio.Event
        self._api: FastAPI
        self._ev_assets: list[EvChargingAsset] = []
        self._ev_contributors: list[EvChargerContributor] = []
        self._threshold_constraints: list[ThresholdConstraints] = []
        self._threshold_contributors: list[ThresholdControlContributor] = []
        self._ev_overrides: dict[str, tuple[float, datetime]] = {}
        self._staged_overrides: dict[str, tuple[float, datetime]] = {}
        self._disabled_chargepoints: set[str] = set()
        self._last_ev_goals: list[EvChargingGoal] = []
        self._mqtt_publisher: EvMqttPublisher | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build all components and launch the three async loops."""
        _log.info("Energy Assistant starting  config=%s  db=%s",
                  self._config_path, self._db_path)

        # 1 — Config
        self._cfg = YamlConfigLoader(self._config_path).load()
        opt = self._cfg.optimizer
        ctl = self._cfg.controller
        self._plan_interval_s = float(ctl.get("plan_interval_s", 3600))
        self._control_interval_s = float(ctl.get("control_interval_s", 30))
        self._poll_interval_s = float(ctl.get("poll_interval_s", self._control_interval_s))
        self._dry_run = bool(ctl.get("dry_run", False)) or os.environ.get("ENERGY_ASSISTANT_DRY_RUN", "") == "1"
        horizon_h = int(opt.get("horizon_hours", 24))
        self._horizon = timedelta(hours=horizon_h)

        # 2 — Build devices / tariffs / topology (shared connection pool)
        ctx = make_build_context(self._cfg)
        self._build_ctx = ctx
        self._registry, self._tariffs, self._topology = build(self._cfg, ctx=ctx)
        _log.info("Loaded %d devices, %d tariffs", len(self._registry), len(self._tariffs))

        # 3 — Forecast providers (top-level + per-device, same ctx)
        self._forecast_providers = build_all_forecasts(self._cfg, ctx=ctx)
        _log.info("Loaded %d forecast providers", len(self._forecast_providers))

        # 4 — Persistent storage
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._storage = SqliteStorageBackend(self._db_path)
        await self._storage.start()

        # 5 — Event bus
        self._bus = EventBus()

        # 6 — Storage constraints + optimizer
        self._storage_constraints = _storage_constraints_from_config(self._cfg)
        self._optimizer = MilpHigsOptimizer(
            step_minutes=int(opt.get("step_minutes", 60)),
            precision=float(opt.get("precision", 0.5)),
        )
        # Coarse-but-instant optimizer for the first pass after startup:
        # publishes a plan within ~a second so the UI is never empty while
        # the configured-precision solve runs.
        self._fast_optimizer = MilpHigsOptimizer(
            step_minutes=int(opt.get("step_minutes", 60)),
            precision=0.0,
        )

        # 7 — Ledger + control loop
        self._ledger = BatteryCostLedger()
        self._control_loop = ControlLoop(ledger=self._ledger)
        for sc in self._storage_constraints:
            self._control_loop.register_contributor(StorageControlContributor(sc))
        _log.info("Registered %d storage contributors", len(self._storage_constraints))

        # 7b — EV assets + contributors
        self._ev_assets = parse_ev_assets(self._cfg.assets)
        self._ev_contributors = [EvChargerContributor(a) for a in self._ev_assets]
        for contrib in self._ev_contributors:
            self._control_loop.register_contributor(contrib)
        # Load persisted overrides and disabled state from SQLite
        self._ev_overrides = await self._storage.load_all_ev_targets()
        self._staged_overrides = dict(self._ev_overrides)  # staged starts from any persisted override
        self._disabled_chargepoints = await self._storage.load_all_ev_disabled()
        for contrib in self._ev_contributors:
            asset = next(a for a in self._ev_assets if a.device_id == contrib.device_id)
            contrib.set_disabled(asset.asset_id in self._disabled_chargepoints)
        _log.info(
            "Loaded %d EV assets (%d overrides, %d disabled)",
            len(self._ev_assets), len(self._ev_overrides), len(self._disabled_chargepoints),
        )

        # 7c — Threshold assets + contributors
        self._threshold_constraints = parse_threshold_assets(self._cfg.assets, self._cfg.devices)
        self._threshold_contributors = [
            ThresholdControlContributor(tc) for tc in self._threshold_constraints
        ]
        for contrib in self._threshold_contributors:
            self._control_loop.register_contributor(contrib)
        _log.info("Loaded %d threshold assets", len(self._threshold_constraints))

        # 7d — MQTT publisher (optional — only started when mqtt: is configured)
        if self._cfg.backends.mqtt and self._ev_assets:
            self._mqtt_publisher = EvMqttPublisher(
                cfg=self._cfg.backends.mqtt,
                assets=self._ev_assets,
                on_stage=self._mqtt_stage_target,
                on_enable_override=self._mqtt_enable_override,
                on_disable_override=self._mqtt_disable_override,
            )
            await self._mqtt_publisher.start()
            _log.info("MQTT publisher started")

        # 8 — Subscribe control loop to plan updates via event bus
        # _plan_seq lets the /api/stream SSE endpoint notify UI clients the
        # moment a new plan is published.
        self._plan_seq = 0

        async def _on_plan_updated(event: PlanUpdatedEvent) -> None:
            self._control_loop.update_plan(event.plan)
            self._plan_seq += 1

        self._bus.subscribe(PlanUpdatedEvent, _on_plan_updated)

        # 9 — Resolve helper lookups
        self._default_tariff = (
            self._tariffs.get(self._cfg.default_tariff_id)
            if self._cfg.default_tariff_id
            else (next(iter(self._tariffs.values()), None))
        )
        self._grid_meter_id = self._topology.device_id if self._topology else None
        self._pv_device_id: str | None = next(
            (d.device_id for d in self._registry.all() if d.role == DeviceRole.PRODUCER),
            None,
        )
        self._pv_opportunity_price = 0.0  # refreshed on first planning cycle
        self._tariff_zones = self._build_tariff_zones()
        _log.info(
            "Tariff zones: %s",
            {zid: (len(z.meter_ids), len(z.producer_ids), len(z.storage_ids))
             for zid, z in self._tariff_zones.items()},
        )
        self._first_poll_done = asyncio.Event()

        if self._dry_run:
            _log.warning("DRY RUN — control commands will be logged but not sent")

        # 10 — Build API + launch loops
        self._api = self._build_api()
        port = int(os.environ.get("ENERGY_ASSISTANT_PORT", "") or self._cfg.server.get("port", 8088))
        _log.info("API listening on http://0.0.0.0:%d", port)
        _log.info("Web UI available at http://localhost:%d", port)
        self.tasks = [
            asyncio.create_task(self._polling_loop(), name="polling"),
            asyncio.create_task(self._planning_loop(), name="planning"),
            asyncio.create_task(self._control_task(), name="control"),
            asyncio.create_task(self._api_task(port), name="api"),
        ]
        _log.info("All loops started")

    async def stop(self) -> None:
        """Cancel all running tasks and close the storage backend."""
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        if self._mqtt_publisher is not None:
            await self._mqtt_publisher.stop()
        if hasattr(self, "_storage"):
            await self._storage.stop()
        _log.info("Energy Assistant stopped")

    async def run_forever(self) -> None:
        """Start and block until all tasks are done (e.g. cancelled by SIGINT)."""
        await self.start()
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # MQTT callbacks (called by EvMqttPublisher on incoming commands)
    # ------------------------------------------------------------------

    async def _mqtt_stage_target(self, asset_id: str, soc: float, deadline: datetime) -> None:
        self._staged_overrides[asset_id] = (soc, deadline)
        _log.info("MQTT staged: %r → %.0f%% by %s", asset_id, soc, deadline)
        # If override is already active, apply the updated staged values immediately
        if asset_id in self._ev_overrides:
            await self._storage.set_ev_target(asset_id, soc, deadline)
            self._ev_overrides[asset_id] = (soc, deadline)
            asyncio.create_task(self._run_plan())

    async def _mqtt_enable_override(self, asset_id: str) -> None:
        staged = self._staged_overrides.get(asset_id)
        if staged is None:
            _log.warning("MQTT enable_override: no staged values for %r", asset_id)
            return
        soc, deadline = staged
        await self._storage.set_ev_target(asset_id, soc, deadline)
        self._ev_overrides[asset_id] = (soc, deadline)
        _log.info("MQTT override enabled: %r → %.0f%% by %s", asset_id, soc, deadline)
        asyncio.create_task(self._run_plan())

    async def _mqtt_disable_override(self, asset_id: str) -> None:
        if asset_id in self._ev_overrides:
            await self._storage.clear_ev_target(asset_id)
            self._ev_overrides.pop(asset_id, None)
            _log.info("MQTT override disabled: %r (staged values kept)", asset_id)
            asyncio.create_task(self._run_plan())

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _polling_loop(self) -> None:
        """Read every device, persist to SQLite, publish DeviceStateEvents."""
        # Warm-up: some devices connect lazily on their first get_state()
        # (e.g. the openWB MQTT bridge).  Poll everything once and give those
        # connections a moment to establish, so the *first* recorded poll —
        # which gates the first plan — already sees EVs as connected.
        # Without this the first plan always ran without EV goals.
        for device in self._registry.all():
            try:
                await device.get_state()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(3.0)

        first_tick = True
        while True:
            states = {}
            for device in self._registry.all():
                try:
                    state = await device.get_state()
                    self._registry.update_state(state)
                    states[device.device_id] = state
                    await self._bus.publish(DeviceStateEvent(state=state))
                    await self._storage.write(
                        Measurement(
                            device_id=state.device_id,
                            timestamp=state.timestamp,
                            power_w=state.power_w,
                            energy_kwh=state.energy_kwh,
                            soc_pct=state.soc_pct,
                            extra=state.extra,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("Polling failed for device %r: %s",
                                 device.device_id, exc)

            await self._bus.flush()

            if first_tick:
                await self._init_ledger(states)
                first_tick = False
                self._first_poll_done.set()  # unblock planning and control loops

            await asyncio.sleep(self._poll_interval_s)

    # ------------------------------------------------------------------
    # Planning loop
    # ------------------------------------------------------------------

    async def _planning_loop(self) -> None:
        """Run the MILP optimizer periodically and publish the resulting plan."""
        await self._first_poll_done.wait()  # ensure registry has live SoC before first run
        while True:
            await self._run_plan()
            await asyncio.sleep(self._plan_interval_s)

    async def _run_plan(self) -> None:
        """Assemble context, optimize, publish plan, refresh price cache."""
        # Refresh cached PV opportunity price
        self._pv_opportunity_price = await _current_export_price(self._tariffs)

        device_states = {
            did: state
            for device in self._registry.all()
            if (state := self._registry.latest_state(device.device_id)) is not None
            for did in (device.device_id,)
        }

        # For storage devices whose cached SoC is unavailable (e.g. ioBroker not
        # ready on startup), poll the device directly to get a fresh reading.
        # This ensures the plan always starts from the same SoC shown in the
        # live view. Falls back to ledger stored energy if the poll also fails.
        for sc in self._storage_constraints:
            state = device_states.get(sc.device_id)
            if state is not None and state.soc_pct is None:
                device = self._registry.get(sc.device_id)
                if device is not None:
                    try:
                        fresh = await device.get_state()
                        if fresh.soc_pct is not None:
                            device_states[sc.device_id] = fresh
                            self._registry.update_state(fresh)
                            _log.info(
                                "_run_plan: fresh SoC poll for %r → %.1f%%",
                                sc.device_id, fresh.soc_pct,
                            )
                            continue
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "_run_plan: fresh poll for %r failed: %s", sc.device_id, exc
                        )
                # Poll also returned None — fall back to ledger stored energy
                if sc.capacity_kwh > 0:
                    stored_kwh = self._ledger.stored_energy(sc.device_id)
                    if stored_kwh is not None:
                        derived_soc = min(100.0, max(0.0, stored_kwh / sc.capacity_kwh * 100.0))
                        device_states[sc.device_id] = state.model_copy(update={"soc_pct": derived_soc})
                        _log.info(
                            "_run_plan: soc_pct missing for %r — using ledger %.2f kWh → %.1f%%",
                            sc.device_id, stored_kwh, derived_soc,
                        )

        forecasts = await _collect_forecasts(self._forecast_providers, self._horizon)

        # Build a tariff-weighted import-price curve from per-consumer load
        # forecasts. This keeps MILP discharge economics aligned with mixed
        # household/heatpump demand instead of a single tariff price.
        weighted_prices, price_estimated_by_ts = await self._build_tariff_weighted_price_forecast()
        if weighted_prices:
            forecasts[ForecastQuantity.PRICE] = weighted_prices
        self._last_price_estimated_by_ts = price_estimated_by_ts

        # UI chart series: keep a dedicated variable-only import price curve
        # (e.g. Tibber spot) so the chart is not blended with flat tariffs.
        (
            self._last_variable_price_pts,
            self._last_variable_price_estimated_by_ts,
        ) = await self._build_ui_variable_price_forecast()

        # Fallback: inject a single tariff curve if we still have no PRICE data.
        # The MILP fetches prices from tariffs internally, but the cached forecasts
        # dict is used by /api/forecast for the UI — it needs prices too.
        if not forecasts.get(ForecastQuantity.PRICE):
            for tariff in self._tariffs.values():
                try:
                    sched = await tariff.price_schedule(self._horizon)
                    if sched and any(tp.price_eur_per_kwh > 0.001 for tp in sched):
                        ext, est_ts = _extend_tariff_schedule_with_daily_repeat(sched, self._horizon)
                        forecasts[ForecastQuantity.PRICE] = [
                            ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh)
                            for tp in ext
                        ]
                        self._last_price_estimated_by_ts = {
                            tp.timestamp: (tp.timestamp in est_ts) for tp in ext
                        }
                        break
                except Exception:  # noqa: BLE001
                    pass

        # Extend PV generation forecast with capped daily-repeat extrapolation.
        pv_points = forecasts.get(ForecastQuantity.PV_GENERATION, [])
        pv_ext, pv_est_ts = _extend_forecast_points_with_daily_repeat(pv_points, self._horizon)
        if pv_ext:
            forecasts[ForecastQuantity.PV_GENERATION] = pv_ext
        self._last_pv_estimated_by_ts = {
            p.timestamp: (p.timestamp in pv_est_ts) for p in pv_ext
        }

        self._last_forecast_pts = forecasts  # cache for /api/forecast

        # Cap the effective horizon at the latest data point actually available.
        # Without this, price arrays are padded with repeated last-known values
        # (nearest-neighbour artefact), which corrupts the p70 terminal-value calc.
        effective_horizon = _infer_effective_horizon(
            forecasts, self._optimizer._step_min, self._horizon
        )

        # Compute active EV goals from current SoC + schedule/overrides,
        # excluding disabled chargepoints from both planning and control.
        active_assets = [a for a in self._ev_assets if a.asset_id not in self._disabled_chargepoints]
        ev_goals = resolve_active_goals(active_assets, device_states, self._ev_overrides)
        self._last_ev_goals = ev_goals
        # Push updated goals to contributors so the control loop uses them.
        # Also propagate the target SoC to devices that write it to hardware
        # (e.g. openWB instant-charging SoC limit register).
        for contrib in self._ev_contributors:
            goal = next((g for g in ev_goals if g.device_id == contrib.device_id), None)
            contrib.update_goal(goal)
            device = self._registry.get(contrib.device_id)
            if device is not None and hasattr(device, "update_target_soc"):
                device.update_target_soc(goal.target_soc_pct if goal else None)
        if ev_goals:
            _log.info(
                "EV goals: %s",
                [(g.asset_id, f"{g.current_soc_pct:.0f}%→{g.target_soc_pct:.0f}%",
                  g.target_by.strftime("%Y-%m-%dT%H:%M")) for g in ev_goals],
            )

        context = OptimizationContext(
            device_states=device_states,
            storage_constraints=self._storage_constraints,
            threshold_constraints=self._threshold_constraints,
            tariffs=self._tariffs,
            forecasts=forecasts,
            horizon=effective_horizon,
            battery_cost_basis=self._ledger.all_cost_bases(),
            ev_charging_goals=ev_goals,
            producer_device_ids={
                d.device_id for d in self._registry.all()
                if d.role == DeviceRole.PRODUCER
            },
        )

        # Two-stage solve: when no plan is active yet (first run after
        # startup), publish a coarse fast-pass plan immediately so the UI
        # has data, then refine with the configured-precision solve below.
        active = self._control_loop._active_plan
        if active is None or not active.intents:
            try:
                fast_plan = await self._fast_optimizer.optimize(context)
                if fast_plan.intents:
                    _log.info(
                        "Fast first-pass plan: %d intents — refining at full precision…",
                        len(fast_plan.intents),
                    )
                    await self._bus.publish(PlanUpdatedEvent(plan=fast_plan))
                    await self._bus.flush()
            except Exception as exc:  # noqa: BLE001
                _log.warning("Fast first-pass optimizer failed: %s", exc)

        try:
            plan = await self._optimizer.optimize(context)
        except Exception as exc:  # noqa: BLE001
            _log.error("Optimizer failed: %s", exc)
            return

        _log.info("New plan: %d intents  horizon=%s  (cap=%s)", len(plan.intents), effective_horizon, self._horizon)
        await self._bus.publish(PlanUpdatedEvent(plan=plan))
        await self._bus.flush()

        if self._mqtt_publisher is not None:
            await self._mqtt_publisher.publish_states(
                ev_goals, device_states, self._ev_overrides, self._staged_overrides
            )

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    async def _control_task(self) -> None:
        """Send setpoints on every control tick based on the active plan."""
        await self._first_poll_done.wait()  # ensure registry and plan are ready

        last = time.monotonic()
        while True:
            now_mono = time.monotonic()
            dt_hours = (now_mono - last) / 3600.0
            last = now_mono

            await self._do_control_tick(dt_hours)
            await asyncio.sleep(self._control_interval_s)

    async def _do_control_tick(self, dt_hours: float) -> None:
        """Build ``LiveSituation`` and call ``ControlLoop.tick()``."""
        now = datetime.now(timezone.utc)

        # Grid power from topology root meter (positive=import, negative=export)
        grid_power_w = 0.0
        if self._grid_meter_id:
            state = self._registry.latest_state(self._grid_meter_id)
            if state is not None and state.power_w is not None:
                grid_power_w = state.power_w

        # PV production (positive = generating)
        pv_power_w = 0.0
        if self._pv_device_id:
            pv_state = self._registry.latest_state(self._pv_device_id)
            if pv_state is not None and pv_state.power_w is not None:
                pv_power_w = abs(pv_state.power_w)

        # Current import price from default tariff
        current_price = 0.0
        if self._default_tariff is not None:
            try:
                current_price = await self._default_tariff.price_at(now)
            except Exception as exc:  # noqa: BLE001
                _log.debug("Could not read current price: %s", exc)

        device_states = {
            device.device_id: state
            for device in self._registry.all()
            if (state := self._registry.latest_state(device.device_id)) is not None
        }

        live = LiveSituation(
            timestamp=now,
            grid_power_w=grid_power_w,
            dt_hours=dt_hours,
            device_states=device_states,
            current_price_eur_per_kwh=current_price,
            pv_opportunity_price_eur_per_kwh=self._pv_opportunity_price,
            pv_power_w=pv_power_w,
            storage_cost_bases=self._ledger.all_cost_bases(),
            default_zone_grid_power_w=self._default_zone_grid_power_w(device_states),
        )

        self._sync_ledger_stored_energy_from_soc()

        if self._dry_run:
            _log.info(
                "DRY RUN tick  grid=%.0f W  price=%.4f €/kWh  dt=%.4f h",
                grid_power_w, current_price, dt_hours,
            )
            for device_id, setpoint_w, mode in self._control_loop.describe_setpoints(live):
                if setpoint_w is None:
                    _log.info(
                        "DRY RUN  %s  mode=%-10s  → skip (no setpoint)",
                        device_id, mode,
                    )
                elif setpoint_w > 0:
                    _log.info(
                        "DRY RUN  %s  mode=%-10s  → charge   %+.0f W",
                        device_id, mode, setpoint_w,
                    )
                elif setpoint_w < 0:
                    _log.info(
                        "DRY RUN  %s  mode=%-10s  → discharge %+.0f W",
                        device_id, mode, setpoint_w,
                    )
                else:
                    _log.info(
                        "DRY RUN  %s  mode=%-10s  → hold (0 W)",
                        device_id, mode,
                    )
            # Persist the current ledger state even in dry_run so the
            # spot-price basis survives the next restart.
            for _sc in self._storage_constraints:
                _basis = self._ledger.cost_basis(_sc.device_id)
                _stored = self._ledger.stored_energy(_sc.device_id)
                if _basis is not None and _stored is not None:
                    await self._storage.save_ledger_state(
                        _sc.device_id,
                        cost_basis=_basis,
                        stored_energy_kwh=_stored,
                    )
                    await self._storage.append_ledger_history(
                        _sc.device_id,
                        cost_basis=_basis,
                        stored_energy_kwh=_stored,
                        timestamp=now,
                    )
            return

        await self._control_loop.tick(live, self._registry)

        # Persist updated ledger state so it survives restarts.
        for sc in self._storage_constraints:
            basis = self._ledger.cost_basis(sc.device_id)
            stored = self._ledger.stored_energy(sc.device_id)
            if basis is not None and stored is not None:
                await self._storage.save_ledger_state(
                    sc.device_id,
                    cost_basis=basis,
                    stored_energy_kwh=stored,
                )
                await self._storage.append_ledger_history(
                    sc.device_id,
                    cost_basis=basis,
                    stored_energy_kwh=stored,
                    timestamp=now,
                )

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    def _build_api(self) -> FastAPI:
        """Build the FastAPI application exposing live server state."""
        api = FastAPI(title="Energy Assistant", version="0.1")

        @api.get("/health")
        async def health() -> dict:
            """Liveness probe endpoint used by container health checks."""
            return {"status": "ok"}

        async def _status_payload() -> dict:
            """Live snapshot: grid power, price, device states, setpoints, ledger.

            Shared by ``GET /api/status`` (poll) and ``GET /api/stream`` (SSE push).
            """
            now = datetime.now(timezone.utc)

            self._sync_ledger_stored_energy_from_soc()

            grid_power_w = 0.0
            if self._grid_meter_id:
                s = self._registry.latest_state(self._grid_meter_id)
                if s and s.power_w is not None:
                    grid_power_w = s.power_w

            current_price = 0.0
            if self._default_tariff is not None:
                try:
                    current_price = await self._default_tariff.price_at(now)
                except Exception:  # noqa: BLE001
                    pass

            device_states_map = {
                d.device_id: st
                for d in self._registry.all()
                if (st := self._registry.latest_state(d.device_id)) is not None
            }

            tariff_prices_status = await self._fetch_tariff_prices(now)
            market_breakdown = self._compute_zone_market_breakdown(
                tariff_prices_status,
                device_states_map,
            )

            live = LiveSituation(
                timestamp=now,
                grid_power_w=grid_power_w,
                dt_hours=0.0,
                device_states=device_states_map,
                current_price_eur_per_kwh=current_price,
                pv_opportunity_price_eur_per_kwh=self._pv_opportunity_price,
                pv_power_w=(
                    abs(ps.power_w)
                    if self._pv_device_id
                    and (ps := self._registry.latest_state(self._pv_device_id)) is not None
                    and ps.power_w is not None
                    else 0.0
                ),
                storage_cost_bases=self._ledger.all_cost_bases(),
                default_zone_grid_power_w=self._default_zone_grid_power_w(device_states_map),
            )

            devices_payload = []
            for s in device_states_map.values():
                cfg = self._cfg.devices.get(s.device_id, {})
                devices_payload.append(
                    {
                        "device_id": s.device_id,
                        "label": cfg.get("label") or s.device_id.replace("_", " ").title(),
                        "role": cfg.get("role"),
                        "power_w": s.power_w,
                        "soc_pct": s.soc_pct,
                        "available": s.available,
                        "timestamp": s.timestamp.isoformat(),
                        "is_virtual": cfg.get("type") == "generic_consumer",
                        "forecast_power_w": await _virtual_forecast_power_w(cfg),
                    }
                )

            return {
                "timestamp": now.isoformat(),
                "grid_power_w": grid_power_w,
                "current_price_eur_per_kwh": current_price,
                "pv_opportunity_price_eur_per_kwh": self._pv_opportunity_price,
                "dry_run": self._dry_run,
                "devices": devices_payload,
                "setpoints": [
                    {
                        "device_id": did,
                        "setpoint_w": sp,
                        "mode": mode,
                        "grid_allowed": intent.grid_allowed if intent is not None else None,
                        "export_allowed": intent.export_allowed if intent is not None else None,
                        "role": (
                            dev.role.value
                            if (dev := self._registry.get(did)) is not None
                            else None
                        ),
                    }
                    for did, sp, mode, intent in self._control_loop._compute_setpoints(live)
                ],
                "ledger": [
                    {
                        "device_id": sc.device_id,
                        "cost_basis_eur_per_kwh": self._ledger.cost_basis(sc.device_id),
                        "stored_energy_kwh": self._ledger.stored_energy(sc.device_id),
                        "capacity_kwh": sc.capacity_kwh,
                    }
                    for sc in self._storage_constraints
                ],
                "market_prices": {
                    tid: float(parts.get("price_eur_per_kwh", 0.0))
                    for tid, parts in market_breakdown.items()
                },
                "market_price_breakdown": market_breakdown,
            }

        @api.get("/api/status")
        async def get_status() -> dict:
            """Live snapshot (poll variant of /api/stream)."""
            return await _status_payload()

        @api.get("/api/stream")
        async def stream(request: Request) -> StreamingResponse:
            """Server-Sent Events: pushes a ``status`` event every few seconds
            and a ``plan`` event whenever a new plan is published, so the UI
            updates without polling."""
            interval_s = 3.0

            async def gen():
                last_plan_seq = self._plan_seq
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        payload = await _status_payload()
                        yield f"event: status\ndata: {json.dumps(payload)}\n\n"
                    except Exception:  # noqa: BLE001
                        _log.debug("SSE status payload failed", exc_info=True)
                    if self._plan_seq != last_plan_seq:
                        last_plan_seq = self._plan_seq
                        yield "event: plan\ndata: {}\n\n"
                    await asyncio.sleep(interval_s)

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "Connection": "keep-alive",
                    # Disable proxy buffering (nginx / HASS ingress) so events
                    # arrive immediately.
                    "X-Accel-Buffering": "no",
                },
            )

        @api.get("/api/plan")
        async def get_plan() -> dict:
            """Active EnergyPlan: all intents with planned power and mode."""
            plan = self._control_loop._active_plan
            if plan is None:
                return {"created_at": None, "step_minutes": self._optimizer._step_min, "intents": []}
            return {
                "created_at": plan.created_at.isoformat(),
                "step_minutes": self._optimizer._step_min,
                # Solved site flows (effective PV incl. live floor, grid
                # import/export) — the UI should use these instead of
                # re-deriving import from the raw forecast series.
                "flows": [
                    {
                        "timestep": f.timestep.isoformat(),
                        "pv_kw": f.pv_kw,
                        "grid_import_kw": f.grid_import_kw,
                        "grid_export_kw": f.grid_export_kw,
                    }
                    for f in plan.flows
                ],
                "intents": [
                    {
                        "device_id": i.device_id,
                        "timestep": i.timestep.isoformat(),
                        "mode": intent_display_mode(i),
                        "planned_kw": i.power_kw,
                        "grid_allowed": i.grid_allowed,
                        "export_allowed": i.export_allowed,
                        "reserved_kwh": i.reserved_kwh,
                        "stored_energy_kwh": i.stored_energy_kwh,
                    }
                    for i in plan.intents
                ],
            }

        @api.post("/api/plan/refresh")
        async def trigger_plan_refresh() -> dict:
            """Trigger an immediate plan recomputation outside the normal interval."""
            await self._run_plan()
            plan = self._control_loop._active_plan
            return {"ok": True, "created_at": plan.created_at.isoformat() if plan else None}

        @api.get("/api/debug/plan_inputs")
        async def debug_plan_inputs() -> dict:
            """Debug: show what the optimizer would receive on the next plan run."""
            from .server import _collect_forecasts, _infer_effective_horizon  # noqa: PLC0415
            device_states = {
                did: state
                for device in self._registry.all()
                if (state := self._registry.latest_state(device.device_id)) is not None
                for did in (device.device_id,)
            }
            forecasts = await _collect_forecasts(self._forecast_providers, self._horizon)
            effective_horizon = _infer_effective_horizon(
                forecasts, self._optimizer._step_min, self._horizon
            )
            return {
                "storage_constraints": [s.device_id for s in self._storage_constraints],
                "device_states": {did: {"soc_pct": s.soc_pct, "available": s.available} for did, s in device_states.items()},
                "forecast_quantities": {str(k): len(v) for k, v in forecasts.items()},
                "effective_horizon_h": effective_horizon.total_seconds() / 3600,
                "n_steps": int(effective_horizon.total_seconds() / (self._optimizer._step_min * 60)),
            }

        @api.get("/api/ledger")
        async def get_ledger() -> list:
            """Battery cost basis and stored energy from the live ledger."""
            self._sync_ledger_stored_energy_from_soc()
            return [
                {
                    "device_id": sc.device_id,
                    "cost_basis_eur_per_kwh": self._ledger.cost_basis(sc.device_id),
                    "stored_energy_kwh": self._ledger.stored_energy(sc.device_id),
                    "capacity_kwh": sc.capacity_kwh,
                }
                for sc in self._storage_constraints
            ]

        @api.post("/api/ledger/set_basis")
        async def set_ledger_basis(
            device_id: str,
            cost_basis_eur_per_kwh: float,
        ) -> dict:
            """Override battery ledger basis from the UI and persist it."""
            if cost_basis_eur_per_kwh < 0.0:
                raise HTTPException(status_code=400, detail="cost_basis_eur_per_kwh must be >= 0")

            sc = next((x for x in self._storage_constraints if x.device_id == device_id), None)
            if sc is None:
                raise HTTPException(status_code=404, detail=f"unknown storage device: {device_id}")

            # Keep stored energy aligned with live SoC before overriding basis.
            self._sync_ledger_stored_energy_from_soc()
            stored = float(self._ledger.stored_energy(device_id) or 0.0)
            self._ledger.initialise(
                device_id,
                stored_energy_kwh=stored,
                cost_basis_eur_per_kwh=float(cost_basis_eur_per_kwh),
            )

            now = datetime.now(timezone.utc)
            await self._storage.save_ledger_state(
                device_id,
                cost_basis=float(cost_basis_eur_per_kwh),
                stored_energy_kwh=stored,
            )
            await self._storage.append_ledger_history(
                device_id,
                cost_basis=float(cost_basis_eur_per_kwh),
                stored_energy_kwh=stored,
                timestamp=now,
            )

            return {
                "device_id": device_id,
                "cost_basis_eur_per_kwh": float(cost_basis_eur_per_kwh),
                "stored_energy_kwh": stored,
                "updated_at": now.isoformat(),
            }

        @api.get("/api/forecast")
        async def get_forecast() -> dict:
            """Last forecast snapshot aligned to the active plan timesteps."""
            plan = self._control_loop._active_plan
            if plan is None or not plan.intents:
                return {"timestamps": [], "prices": [], "export_prices": [],
                        "pv_kw": [], "consumption_kw": [], "step_minutes": self._optimizer._step_min,
                        "storage_capacity": {}}

            # Deduplicate and sort plan timestamps
            timestamps = sorted({i.timestep for i in plan.intents})
            pts_price = sorted(self._last_forecast_pts.get(ForecastQuantity.PRICE, []),
                               key=lambda p: p.timestamp)
            pts_var_price = sorted(self._last_variable_price_pts, key=lambda p: p.timestamp)
            pts_pv    = sorted(self._last_forecast_pts.get(ForecastQuantity.PV_GENERATION, []),
                               key=lambda p: p.timestamp)
            pts_cons_raw = self._last_forecast_pts.get(ForecastQuantity.CONSUMPTION, [])

            def nn_value(pts: list[ForecastPoint], ts: datetime) -> float:
                if not pts:
                    return 0.0
                best = min(pts, key=lambda p: abs((p.timestamp - ts).total_seconds()))
                return float(best.value)

            def ff_value(pts: list[ForecastPoint], ts: datetime) -> float:
                """Forward-fill value at *ts* (piecewise-constant, left-hold).

                For PV this avoids nearest-neighbour artefacts around night gaps
                (e.g. midnight accidentally snapping to sunrise values).
                """
                if not pts:
                    return 0.0
                if ts < pts[0].timestamp:
                    return 0.0
                prev = pts[0]
                for p in pts:
                    if p.timestamp > ts:
                        break
                    prev = p
                return float(prev.value)

            def nn_flag(flags_by_ts: dict[datetime, bool], ts: datetime) -> bool:
                """Nearest-timestamp mapping for estimated flags.

                Plan timestamps are often 15-min while source schedules may be
                hourly, so exact dict lookup can miss flags and hide dashed lines.
                """
                if not flags_by_ts:
                    return False
                nearest_ts = min(flags_by_ts.keys(), key=lambda t: abs((t - ts).total_seconds()))
                return bool(flags_by_ts.get(nearest_ts, False))

            # Consumption: multiple providers produce duplicate timestamps — sum them.
            # Strategy: group all raw points by their original timestamp, sum across
            # providers, then nearest-neighbour interpolate onto plan timestamps.
            # This is correct regardless of whether the plan step < provider step (e.g.
            # 15-min plan steps with hourly consumption profiles) because NN fills every
            # plan step from the closest available summed value.
            from collections import defaultdict
            cons_by_ts: dict[datetime, float] = defaultdict(float)
            for pt in pts_cons_raw:
                cons_by_ts[pt.timestamp] += float(pt.value)
            cons_pts_summed = sorted(
                [ForecastPoint(timestamp=ts, value=v) for ts, v in cons_by_ts.items()],
                key=lambda p: p.timestamp,
            )

            # Also fetch export prices from tariff (flat scalar is inaccurate when
            # the export tariff itself has a schedule, e.g. Tibber spot export).
            ep_pts: list[ForecastPoint] = []
            for tariff in self._tariffs.values():
                try:
                    sched: list[TariffPoint] = await tariff.export_price_schedule(self._horizon)
                    if sched and any(tp.price_eur_per_kwh > 0.001 for tp in sched):
                        ep_pts = sorted(
                            [ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh)
                             for tp in sched],
                            key=lambda p: p.timestamp,
                        )
                        break
                except Exception:  # noqa: BLE001
                    pass

            def ep_value(ts: datetime) -> float:
                if ep_pts:
                    return nn_value(ep_pts, ts)
                return float(self._pv_opportunity_price)

            return {
                "timestamps":    [t.isoformat() for t in timestamps],
                "prices":        [nn_value(pts_price, t) for t in timestamps],
                "price_is_estimated": [nn_flag(self._last_price_estimated_by_ts, t) for t in timestamps],
                "variable_prices": [nn_value(pts_var_price, t) for t in timestamps],
                "variable_price_is_estimated": [
                    nn_flag(self._last_variable_price_estimated_by_ts, t) for t in timestamps
                ],
                "export_prices": [ep_value(t) for t in timestamps],
                "pv_kw":         [ff_value(pts_pv, t) for t in timestamps],
                "pv_is_estimated": [nn_flag(self._last_pv_estimated_by_ts, t) for t in timestamps],
                "consumption_kw": [nn_value(cons_pts_summed, t) for t in timestamps],
                "step_minutes":  self._optimizer._step_min,
                "storage_capacity": {
                    sc.device_id: {
                        "capacity_kwh":         sc.capacity_kwh,
                        "min_soc_pct":          sc.min_soc_pct,
                        "max_soc_pct":          sc.max_soc_pct,
                        "charge_efficiency":    sc.charge_efficiency,
                        "discharge_efficiency": sc.discharge_efficiency,
                    }
                    for sc in self._storage_constraints
                },
            }

        @api.get("/api/history")
        async def get_history(hours: float = 24.0, device_ids: str = "") -> dict:
            """Historical measurements from SQLite.

            Query parameters
            ----------------
            hours:       look-back window (default 24).
            device_ids:  comma-separated device IDs.  Defaults to all storage
                         devices + the topology root meter + PV producers.
            """
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=min(hours, 168.0))  # max 7 days

            if device_ids.strip():
                ids = [d.strip() for d in device_ids.split(",") if d.strip()]
            else:
                # Default: storage + meter + producer devices
                ids = [sc.device_id for sc in self._storage_constraints]
                for device in self._registry.all():
                    if device.role in (DeviceRole.METER, DeviceRole.PRODUCER):
                        ids.append(device.device_id)
                ids = list(dict.fromkeys(ids))  # deduplicate preserving order

            result: dict[str, list] = {}
            for did in ids:
                rows = await self._storage.query(did, start, now)
                result[did] = [
                    {
                        "t": r.timestamp.isoformat(),
                        "power_w": r.power_w,
                        "soc_pct": r.soc_pct,
                    }
                    for r in rows
                ]

            # Include ledger history for storage devices
            ledger_hist: dict[str, list] = {}
            for sc in self._storage_constraints:
                ledger_hist[sc.device_id] = await self._storage.query_ledger_history(
                    sc.device_id, start, now
                )

            return {"measurements": result, "ledger": ledger_hist}

        @api.get("/api/config")
        async def get_config() -> dict:
            """Static device configuration: roles + storage parameters."""
            devices = []
            for device in self._registry.all():
                devices.append({
                    "device_id": device.device_id,
                    "role": device.role.value if hasattr(device.role, "value") else str(device.role),
                })
            return {
                "devices": devices,
                "storage_constraints": [
                    {
                        "device_id":            sc.device_id,
                        "capacity_kwh":         sc.capacity_kwh,
                        "min_soc_pct":          sc.min_soc_pct,
                        "max_soc_pct":          sc.max_soc_pct,
                        "charge_efficiency":    sc.charge_efficiency,
                        "discharge_efficiency": sc.discharge_efficiency,
                        "max_charge_kw":        sc.max_charge_kw,
                        "max_discharge_kw":     sc.max_discharge_kw,
                    }
                    for sc in self._storage_constraints
                ],
            }

        # ── EV charging targets ────────────────────────────────────────

        @api.get("/api/ev")
        async def get_ev_status() -> list[dict]:
            """Active EV charging goals (one per configured chargepoint)."""
            device_states = {
                did: state
                for device in self._registry.all()
                if (state := self._registry.latest_state(device.device_id)) is not None
                for did in (device.device_id,)
            }
            # Compute fresh goals so target always reflects the current override/schedule,
            # not the (potentially stale) last plan run.
            fresh_goals = resolve_active_goals(self._ev_assets, device_states, self._ev_overrides)
            result = []
            for asset in self._ev_assets:
                state = self._registry.latest_state(asset.device_id)
                goal = next((g for g in fresh_goals if g.asset_id == asset.asset_id), None)
                # phase1/phase2 kWh come from the last optimizer run (more accurate energy split)
                planned = next((g for g in self._last_ev_goals if g.asset_id == asset.asset_id), None)
                result.append({
                    "asset_id":       asset.asset_id,
                    "device_id":      asset.device_id,
                    "label":          asset.label,
                    "connected":      state.available if state else False,
                    "soc_pct":        state.soc_pct if state else None,
                    "charge_limit_soc_pct": asset.charge_limit_soc_pct,
                    "max_charge_kw":  asset.max_charge_kw,
                    "goal": {
                        "target_soc_pct":   goal.target_soc_pct,
                        "target_by":        goal.target_by.isoformat(),
                        "phase1_kwh":       round(planned.phase1_required_kwh, 2) if planned else 0.0,
                        "phase2_kwh":       round(planned.phase2_required_kwh, 2) if planned else 0.0,
                        "phase2_start":     planned.phase2_start_time.isoformat() if planned else goal.target_by.isoformat(),
                    } if goal else None,
                    "staged": {
                        "target_soc_pct": override[0],
                        "target_by":      override[1].isoformat(),
                    } if (override := self._staged_overrides.get(asset.asset_id)) else None,
                    "override_active": asset.asset_id in self._ev_overrides,
                    "disabled": asset.asset_id in self._disabled_chargepoints,
                })
            return result

        # ── Controllable devices (EV chargers + storage + threshold loads) ──

        @api.get("/api/controllable")
        async def get_controllable() -> dict:
            """Plan data for all controllable devices."""
            plan = self._control_loop._active_plan
            now = datetime.now(timezone.utc)
            step_min: int = self._optimizer._step_min

            def _sorted_intents(device_id: str) -> list:
                if plan is None:
                    return []
                return sorted(
                    [i for i in plan.intents if i.device_id == device_id],
                    key=lambda i: i.timestep,
                )

            def _next_change(steps_from_active: list) -> dict | None:
                """First upcoming mode different from the ACTIVE slot's mode.

                ``steps_from_active[0]`` must be the currently active slot
                (greatest timestamp ≤ now) so the comparison is anchored to
                what the device is doing right now, not to the next slot.
                Works with both ControlIntent objects and plain step dicts.
                """
                if not steps_from_active:
                    return None
                def _mode(s):  return s["mode"] if isinstance(s, dict) else s.mode
                def _ts(s):    return datetime.fromisoformat(s["ts"]) if isinstance(s, dict) else s.timestep
                current = _mode(steps_from_active[0])
                for step in steps_from_active[1:]:
                    m = _mode(step)
                    if m != current:
                        delta_min = int((_ts(step) - now).total_seconds() / 60)
                        return {"mode": m, "in_minutes": max(0, delta_min)}
                return None

            def _split_active(steps: list[dict]) -> tuple[dict | None, list[dict]]:
                """Return (active_step, steps_from_active) for plain step dicts.

                The active slot is the one with the greatest timestamp ≤ now —
                slot timestamps mark slot *starts*; same semantics as the
                control loop's ``_find_intent()``.
                """
                past   = [s for s in steps if datetime.fromisoformat(s["ts"]) <= now]
                future = [s for s in steps if datetime.fromisoformat(s["ts"]) > now]
                active = past[-1] if past else None
                return active, ([active] if active else []) + future

            # All plan timestamps in order (used to fill EV idle gaps)
            all_plan_ts: list[datetime] = sorted(
                {i.timestep for i in plan.intents} if plan else []
            )
            step_h: float = step_min / 60.0

            devices = []

            # ── EV chargers ─────────────────────────────────────────────
            for asset in self._ev_assets:
                state = self._registry.latest_state(asset.device_id)
                soc_pct = state.soc_pct if state is not None else None
                power_w = state.power_w if state is not None else None
                connected = state.available if state is not None else False

                # EV intents are sparse (only charging steps); build a lookup
                # and walk every plan timestamp to reconstruct the SoC trajectory.
                intent_by_ts = {i.timestep: i for i in _sorted_intents(asset.device_id)}
                ev_goal = next((g for g in self._last_ev_goals if g.asset_id == asset.asset_id), None)
                phase2_start = ev_goal.phase2_start_time if ev_goal else None
                energy_kwh = (soc_pct or 0.0) / 100.0 * asset.capacity_kwh
                cap_limit = asset.capacity_kwh * asset.charge_limit_soc_pct / 100.0
                steps = []
                for ts in all_plan_ts:
                    intent = intent_by_ts.get(ts)
                    if intent is None:
                        mode = "idle"
                    elif phase2_start is not None and ts >= phase2_start:
                        mode = "charge_phase2"
                    else:
                        mode = intent_display_mode(intent)
                    planned_kw = intent.power_kw if intent else 0.0
                    if planned_kw > 0:
                        energy_kwh = min(cap_limit, energy_kwh + planned_kw * step_h)
                    steps.append({
                        "ts": ts.isoformat(),
                        "mode": mode,
                        "planned_kw": planned_kw,
                        "soc_pct": round(energy_kwh / asset.capacity_kwh * 100, 1),
                    })

                active, from_active = _split_active(steps)
                current_mode = (
                    active["mode"] if active
                    else (from_active[0]["mode"] if from_active else "idle")
                )

                devices.append({
                    "device_id": asset.device_id,
                    "label": asset.label,
                    "type": "ev",
                    "capacity_kwh": asset.capacity_kwh,
                    "soc_pct": soc_pct,
                    "connected": connected,
                    "power_w": power_w,
                    "plan": {
                        "current_mode": current_mode,
                        "steps": steps,
                        "next_change": _next_change(from_active),
                    },
                })

            # ── Storage devices ─────────────────────────────────────────
            for sc in self._storage_constraints:
                state = self._registry.latest_state(sc.device_id)
                soc_pct = state.soc_pct if state is not None else None
                power_w = state.power_w if state is not None else None

                all_intents = _sorted_intents(sc.device_id)

                steps = [
                    {
                        "ts": i.timestep.isoformat(),
                        "mode": intent_display_mode(i),
                        "planned_kw": i.power_kw,
                        "soc_pct": (
                            round(i.stored_energy_kwh / sc.capacity_kwh * 100, 1)
                            if i.stored_energy_kwh is not None else None
                        ),
                    }
                    for i in all_intents
                ]
                active, from_active = _split_active(steps)
                current_mode = (
                    active["mode"] if active
                    else (from_active[0]["mode"] if from_active else "idle")
                )

                devices.append({
                    "device_id": sc.device_id,
                    "label": sc.device_id.replace("_", " ").title(),
                    "type": "storage",
                    "capacity_kwh": sc.capacity_kwh,
                    "min_soc_pct": sc.min_soc_pct,
                    "max_soc_pct": sc.max_soc_pct,
                    "soc_pct": soc_pct,
                    "power_w": power_w,
                    "plan": {
                        "current_mode": current_mode,
                        "steps": steps,
                        "next_change": _next_change(from_active),
                    },
                })

            # ── Threshold devices ───────────────────────────────────────
            for tc in self._threshold_constraints:
                state = self._registry.latest_state(tc.device_id)
                current_value: float | None = None
                power_w: float | None = None
                if state is not None:
                    raw = state.extra.get("measured_value")
                    current_value = float(raw) if raw is not None else None
                    power_w = state.power_w

                all_intents = _sorted_intents(tc.device_id)

                steps = [
                    {
                        "ts": i.timestep.isoformat(),
                        "mode": intent_display_mode(i, is_threshold=True),
                        "planned_kw": i.power_kw,
                        "predicted_value": i.stored_energy_kwh,
                    }
                    for i in all_intents
                ]
                active, from_active = _split_active(steps)
                current_mode = (
                    active["mode"] if active
                    else (from_active[0]["mode"] if from_active else "standby")
                )

                devices.append({
                    "device_id": tc.device_id,
                    "label": tc.label or tc.device_id.replace("_", " ").title(),
                    "type": "threshold",
                    "unit": tc.unit,
                    "bottom_threshold": tc.bottom_threshold,
                    "top_threshold": tc.top_threshold,
                    "direction": tc.direction,
                    "rated_power_kw": tc.rated_power_kw,
                    "current_value": current_value,
                    "power_w": power_w,
                    "plan": {
                        "current_mode": current_mode,
                        "steps": steps,
                        "next_change": _next_change(from_active),
                    },
                })

            return {"devices": devices, "step_minutes": step_min}

        def _parse_ev_asset(asset_id: str) -> EvChargingAsset:
            asset = next((a for a in self._ev_assets if a.asset_id == asset_id), None)
            if asset is None:
                raise HTTPException(404, f"Unknown EV asset: {asset_id!r}")
            return asset

        def _parse_target_dt(target_by: str) -> datetime:
            try:
                dt = datetime.fromisoformat(target_by)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(400, f"Invalid target_by datetime: {target_by!r}")

        @api.post("/api/ev/{asset_id}/stage")
        async def stage_ev_target(
            asset_id: str,
            target_soc_pct: float,
            target_by: str,
        ) -> dict:
            """Store staged override values without activating the override.

            Values are held in memory and applied to the optimizer only when
            ``POST /api/ev/{asset_id}/override`` is called.
            """
            _parse_ev_asset(asset_id)
            if not (0 <= target_soc_pct <= 100):
                raise HTTPException(400, "target_soc_pct must be 0–100")
            target_dt = _parse_target_dt(target_by)
            self._staged_overrides[asset_id] = (target_soc_pct, target_dt)
            # If override is already active, update it immediately too
            if asset_id in self._ev_overrides:
                await self._storage.set_ev_target(asset_id, target_soc_pct, target_dt)
                self._ev_overrides[asset_id] = (target_soc_pct, target_dt)
                asyncio.create_task(self._run_plan())
            _log.info("EV staged: %r → %.0f%% by %s", asset_id, target_soc_pct, target_dt)
            return {"status": "ok", "asset_id": asset_id,
                    "target_soc_pct": target_soc_pct, "target_by": target_dt.isoformat()}

        @api.post("/api/ev/{asset_id}/override")
        async def enable_ev_override(asset_id: str) -> dict:
            """Apply the staged values as an active override (feeds the optimizer)."""
            _parse_ev_asset(asset_id)
            staged = self._staged_overrides.get(asset_id)
            if staged is None:
                raise HTTPException(400, f"No staged values for asset: {asset_id!r}")
            soc, dt = staged
            await self._storage.set_ev_target(asset_id, soc, dt)
            self._ev_overrides[asset_id] = (soc, dt)
            _log.info("EV override enabled: %r → %.0f%% by %s", asset_id, soc, dt)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "asset_id": asset_id, "override_active": True}

        @api.delete("/api/ev/{asset_id}/override")
        async def disable_ev_override(asset_id: str) -> dict:
            """Deactivate the override (reverts to schedule). Staged values are kept."""
            _parse_ev_asset(asset_id)
            if asset_id not in self._ev_overrides:
                raise HTTPException(404, f"No active override for asset: {asset_id!r}")
            await self._storage.clear_ev_target(asset_id)
            self._ev_overrides.pop(asset_id, None)
            _log.info("EV override disabled: %r (staged kept)", asset_id)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "asset_id": asset_id, "override_active": False}

        @api.post("/api/ev/{asset_id}/set_target")
        async def set_ev_target(
            asset_id: str,
            target_soc_pct: float,
            target_by: str,
        ) -> dict:
            """Stage and immediately activate an override (legacy endpoint)."""
            _parse_ev_asset(asset_id)
            if not (0 <= target_soc_pct <= 100):
                raise HTTPException(400, "target_soc_pct must be 0–100")
            target_dt = _parse_target_dt(target_by)
            self._staged_overrides[asset_id] = (target_soc_pct, target_dt)
            await self._storage.set_ev_target(asset_id, target_soc_pct, target_dt)
            self._ev_overrides[asset_id] = (target_soc_pct, target_dt)
            _log.info("EV override set: %r → %.0f%% by %s", asset_id, target_soc_pct, target_dt)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "asset_id": asset_id,
                    "target_soc_pct": target_soc_pct, "target_by": target_dt.isoformat()}

        @api.delete("/api/ev/{asset_id}/target")
        async def clear_ev_target(asset_id: str) -> dict:
            """Clear override and staged values (legacy endpoint)."""
            if asset_id not in self._ev_overrides:
                raise HTTPException(404, f"No override for asset: {asset_id!r}")
            await self._storage.clear_ev_target(asset_id)
            self._ev_overrides.pop(asset_id, None)
            self._staged_overrides.pop(asset_id, None)
            _log.info("EV override + staged cleared: %r", asset_id)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "asset_id": asset_id}

        @api.post("/api/ev/{asset_id}/disable")
        async def disable_chargepoint(asset_id: str) -> dict:
            """Exclude this chargepoint from optimizer and control."""
            if not any(a.asset_id == asset_id for a in self._ev_assets):
                raise HTTPException(404, f"Unknown EV asset: {asset_id!r}")
            await self._storage.set_ev_disabled(asset_id)
            self._disabled_chargepoints.add(asset_id)
            for contrib in self._ev_contributors:
                asset = next((a for a in self._ev_assets if a.device_id == contrib.device_id), None)
                if asset and asset.asset_id == asset_id:
                    contrib.set_disabled(True)
            _log.info("Chargepoint disabled: %r", asset_id)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "asset_id": asset_id, "disabled": True}

        @api.delete("/api/ev/{asset_id}/disable")
        async def enable_chargepoint(asset_id: str) -> dict:
            """Re-include this chargepoint in optimizer and control."""
            if not any(a.asset_id == asset_id for a in self._ev_assets):
                raise HTTPException(404, f"Unknown EV asset: {asset_id!r}")
            await self._storage.clear_ev_disabled(asset_id)
            self._disabled_chargepoints.discard(asset_id)
            for contrib in self._ev_contributors:
                asset = next((a for a in self._ev_assets if a.device_id == contrib.device_id), None)
                if asset and asset.asset_id == asset_id:
                    contrib.set_disabled(False)
            _log.info("Chargepoint enabled: %r", asset_id)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "asset_id": asset_id, "disabled": False}

        # ── Static frontend (registered last so API routes take priority) ──────
        # JS files are served at /ui/…; index.html is served at /.
        # Cache-Control: no-store on every response so browser always fetches
        # fresh JS after a server restart (no stale module cache issues).
        # Locate the frontend directory. Two layouts are supported:
        #   installed wheel: energy_assistant/frontend/ sits next to this package
        #   dev repo:        frontend/ is at the repository root (4 levels up)
        _pkg_root = Path(__file__).resolve().parent.parent  # .../energy_assistant/
        _fe = next(
            (p for p in [
                _pkg_root / "frontend",                       # installed wheel
                _pkg_root.parent.parent / "frontend",          # dev repo root
            ] if (p / "index.html").exists()),
            None,
        )
        if _fe is not None:
            from fastapi.responses import FileResponse
            from starlette.middleware.base import BaseHTTPMiddleware

            class _NoCacheMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    response = await call_next(request)
                    if request.url.path.startswith("/ui/"):
                        response.headers["Cache-Control"] = "no-store"
                    return response

            api.add_middleware(_NoCacheMiddleware)
            api.mount("/ui", StaticFiles(directory=str(_fe)), name="ui")

            @api.get("/")
            async def serve_ui() -> FileResponse:
                return FileResponse(str(_fe / "index.html"), headers={"Cache-Control": "no-store"})
        else:
            @api.get("/", response_class=HTMLResponse)
            async def serve_ui_missing() -> str:
                return "<h1>UI not found. Place <code>frontend/index.html</code> at the repo root.</h1>"

        return api

    async def _api_task(self, port: int) -> None:
        """Run the FastAPI app under uvicorn, sharing the existing event loop."""
        config = uvicorn.Config(
            self._api, host="0.0.0.0", port=port, log_level="warning"
        )
        server = uvicorn.Server(config)
        # Prevent uvicorn from overriding the SIGINT/SIGTERM handlers
        # registered by __main__.py.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        await server.serve()

    # ------------------------------------------------------------------
    # Ledger initialisation
    # ------------------------------------------------------------------

    async def _init_ledger(self, states: dict[str, Any]) -> None:
        """Initialise the ledger from persisted state or live SoC readings.

        Lookup order per device
        -----------------------
        1. ``ledger_state`` table in SQLite — the saved basis and stored energy
           from the previous run.  This is the normal case after the first start.
        2. First start (no persisted row): use live SoC for ``stored_energy_kwh``
           and the current spot price for ``cost_basis``.  Using the current
           spot price is the most conservative sensible assumption — we don't
           know what the stored energy actually cost, so we assume it cost what
           it costs to buy right now.  The ledger will converge to real prices
           as soon as the battery cycles through its first charge event.
        """
        # Fetch current spot price once for first-start basis initialisation.
        now = datetime.now(timezone.utc)
        spot_price = 0.0
        if self._default_tariff is not None:
            try:
                spot_price = await self._default_tariff.price_at(now)
            except Exception as exc:  # noqa: BLE001
                _log.warning("_init_ledger: could not fetch spot price: %s", exc)

        for sc in self._storage_constraints:
            persisted = await self._storage.load_ledger_state(sc.device_id)

            if persisted is not None:
                cost_basis, stored_kwh = persisted
                if cost_basis < 0.001:
                    # Basis below 0.1 ct/kWh is effectively zero — leftover
                    # from a run before the first-start fix.  Reinitialise.
                    _log.info(
                        "Ledger persisted basis=0 for %r — reinitialising from spot price",
                        sc.device_id,
                    )
                    state = states.get(sc.device_id)
                    soc_pct = (state.soc_pct if state and state.soc_pct is not None else 0.0)
                    stored_kwh = sc.capacity_kwh * soc_pct / 100.0
                    cost_basis = spot_price
                    _log.info(
                        "Ledger init (zero-basis reset)  %r  soc=%.1f%%  stored=%.2f kWh  basis=%.4f \u20ac/kWh  (spot price)",
                        sc.device_id, soc_pct, stored_kwh, cost_basis,
                    )
                else:
                    _log.info(
                        "Ledger restored  %r  stored=%.2f kWh  basis=%.4f \u20ac/kWh",
                        sc.device_id, stored_kwh, cost_basis,
                    )
            else:
                # First start — no history.  Use live SoC and current spot price.
                state = states.get(sc.device_id)
                soc_pct = (state.soc_pct if state and state.soc_pct is not None else 0.0)
                stored_kwh = sc.capacity_kwh * soc_pct / 100.0
                cost_basis = spot_price
                _log.info(
                    "Ledger init (first start)  %r  soc=%.1f%%  stored=%.2f kWh  basis=%.4f \u20ac/kWh  (spot price)",
                    sc.device_id, soc_pct, stored_kwh, cost_basis,
                )

            self._ledger.initialise(
                sc.device_id,
                stored_energy_kwh=stored_kwh,
                cost_basis_eur_per_kwh=cost_basis,
            )

    def _sync_ledger_stored_energy_from_soc(self) -> None:
        """Keep ledger stored energy anchored to live SoC readings."""
        for sc in self._storage_constraints:
            state = self._registry.latest_state(sc.device_id)
            if state is None or state.soc_pct is None:
                continue
            stored_kwh = sc.capacity_kwh * float(state.soc_pct) / 100.0
            self._ledger.set_stored_energy(sc.device_id, stored_kwh)

    # ------------------------------------------------------------------
    # Tariff-zone helpers
    # ------------------------------------------------------------------

    def _build_tariff_zones(self) -> "dict[str, _TariffZone]":
        """Build per-zone circuit maps driven by the topology tree.

        The topology is the authoritative source for zone meters: only
        METER-role devices that appear as *non-root* topology nodes become
        zone meters.  Their subtrees define which PRODUCER and STORAGE
        devices belong to that zone.

        Any PRODUCER or STORAGE device that does not appear in the topology
        falls back to its ``tariff`` config key (or the site-wide default
        tariff) so that devices wired outside the explicitly modelled tree
        are still attributed correctly.

        The topology root (Z1) is excluded from zone building — it is the
        site-level measurement point, not a price zone.
        """
        default_tariff = self._cfg.default_tariff_id or ""
        zones: dict[str, _TariffZone] = {}
        claimed: set[str] = set()

        if self._topology:
            # Exclude root from zone building (Z1 = site meter, not a price zone)
            claimed.add(self._topology.device_id)
            for child_node in self._topology.children:
                self._collect_zone_from_node(child_node, zones, claimed, default_tariff)

        # Fallback: PRODUCER/STORAGE devices not captured by topology subtrees
        for device in self._registry.all():
            if device.device_id in claimed:
                continue
            dev_cfg = self._cfg.devices.get(device.device_id, {})
            tariff_id = dev_cfg.get("tariff") or default_tariff
            if not tariff_id:
                continue
            zone = zones.setdefault(tariff_id, _TariffZone(tariff_id))
            if device.role == DeviceRole.PRODUCER:
                zone.producer_ids.append(device.device_id)
            elif device.role == DeviceRole.STORAGE:
                zone.storage_ids.append(device.device_id)

        # Differential consumers: heatpump = Z1 − Z2.
        # Build a zone that knows its minuend (Z1) and subtrahend (Z2) so
        # _compute_zone_market_prices can blend the flat grid rate with any
        # Z2 feedback (household PV/battery surplus flowing into heatpump).
        for device in self._registry.all():
            if device.role != DeviceRole.CONSUMER:
                continue
            dev_cfg = self._cfg.devices.get(device.device_id, {})
            if dev_cfg.get("type") != "differential":
                continue
            tariff_id = dev_cfg.get("tariff") or default_tariff
            if not tariff_id:
                continue
            minuend_id = dev_cfg.get("minuend")
            subtrahend_id = dev_cfg.get("subtrahend")
            if not minuend_id or not subtrahend_id:
                continue
            zone = zones.setdefault(tariff_id, _TariffZone(tariff_id))
            zone.diff_minuend_id = minuend_id
            zone.diff_subtrahend_id = subtrahend_id

        return zones

    def _collect_zone_from_node(
        self,
        node: TopologyNode,
        zones: "dict[str, _TariffZone]",
        claimed: "set[str]",
        default_tariff: str,
    ) -> None:
        """Recursively walk a topology sub-tree and assign devices to zones.

        A METER-role node defines the zone: it becomes that zone's grid
        reference and owns all PRODUCER/STORAGE devices in its subtree.
        A non-METER node (e.g. a derived consumer) is skipped but its
        children are still traversed so nested sub-circuits are found.
        """
        device = self._registry.get(node.device_id)
        if device is None:
            for child in node.children:
                self._collect_zone_from_node(child, zones, claimed, default_tariff)
            return

        claimed.add(node.device_id)

        if device.role == DeviceRole.METER:
            dev_cfg = self._cfg.devices.get(device.device_id, {})
            tariff_id = dev_cfg.get("tariff") or default_tariff
            if tariff_id:
                zone = zones.setdefault(tariff_id, _TariffZone(tariff_id))
                zone.meter_ids.append(device.device_id)
                for child in node.children:
                    self._collect_subtree_for_zone(child, zone, claimed)
        else:
            # Non-meter node (consumer, etc.) — recurse for nested sub-zones
            for child in node.children:
                self._collect_zone_from_node(child, zones, claimed, default_tariff)

    def _collect_subtree_for_zone(
        self,
        node: TopologyNode,
        zone: "_TariffZone",
        claimed: "set[str]",
    ) -> None:
        """Add all PRODUCER/STORAGE devices in *node*'s subtree to *zone*.

        Stops recursing into nested METER nodes — those define their own zones.
        """
        device = self._registry.get(node.device_id)
        claimed.add(node.device_id)
        if device is not None:
            if device.role == DeviceRole.PRODUCER:
                zone.producer_ids.append(device.device_id)
            elif device.role == DeviceRole.STORAGE:
                zone.storage_ids.append(device.device_id)
            elif device.role == DeviceRole.METER:
                # Nested meter — will be its own zone; don't claim for parent
                claimed.discard(node.device_id)
                return
        for child in node.children:
            self._collect_subtree_for_zone(child, zone, claimed)

    def _compute_zone_market_prices(
        self,
        tariff_prices: dict[str, float],
        device_states: "dict[str, Any]",
    ) -> dict[str, float]:
        """Compute blended market price per tariff zone (€/kWh)."""
        breakdown = self._compute_zone_market_breakdown(tariff_prices, device_states)
        return {
            tariff_id: float(parts.get("price_eur_per_kwh", 0.0))
            for tariff_id, parts in breakdown.items()
        }

    def _compute_zone_market_breakdown(
        self,
        tariff_prices: dict[str, float],
        device_states: "dict[str, Any]",
    ) -> dict[str, dict[str, float]]:
        """Compute per-zone market-price components for UI diagnostics.

        Returns, per tariff ID:

        - ``price_eur_per_kwh``
        - ``grid_frac`` / ``pv_frac`` / ``bat_frac``
        - ``grid_w`` / ``pv_w`` / ``bat_w``

        Fractions always sum to approximately 1.0 when total power is > 0.
        """

        def _parts(price: float, grid_w: float, pv_w: float, bat_w: float) -> dict[str, float]:
            total_w = max(0.0, grid_w) + max(0.0, pv_w) + max(0.0, bat_w)
            if total_w <= 0.0:
                return {
                    "price_eur_per_kwh": float(price),
                    "grid_frac": 1.0,
                    "pv_frac": 0.0,
                    "bat_frac": 0.0,
                    "grid_w": max(0.0, grid_w),
                    "pv_w": max(0.0, pv_w),
                    "bat_w": max(0.0, bat_w),
                }
            return {
                "price_eur_per_kwh": float(price),
                "grid_frac": max(0.0, grid_w) / total_w,
                "pv_frac": max(0.0, pv_w) / total_w,
                "bat_frac": max(0.0, bat_w) / total_w,
                "grid_w": max(0.0, grid_w),
                "pv_w": max(0.0, pv_w),
                "bat_w": max(0.0, bat_w),
            }
        result: dict[str, float] = {}
        breakdown: dict[str, dict[str, float]] = {}
        cost_bases = self._ledger.all_cost_bases()

        # ── Pass 1: direct zones ──────────────────────────────────────────────
        for tariff_id, import_price in tariff_prices.items():
            if import_price <= 0.0:
                continue
            zone = self._tariff_zones.get(tariff_id)

            # No zone object (or zone with no local renewables/storage) →
            # may be a pure differential zone, handled in pass 2.
            if zone is None:
                continue
            if zone.diff_minuend_id is not None and not zone.meter_ids:
                # Purely differential — skip to pass 2
                continue

            if not zone.producer_ids and not zone.storage_ids:
                # Direct zone but no renewables → always 100 % grid rate
                result[tariff_id] = import_price
                breakdown[tariff_id] = _parts(import_price, grid_w=1.0, pv_w=0.0, bat_w=0.0)
                continue

            # Clamped grid import for this zone's sub-meter
            grid_w = max(0.0, sum(
                (s.power_w or 0.0)
                for did in zone.meter_ids
                if (s := device_states.get(did)) is not None and s.power_w is not None
            ))

            pv_w = sum(
                abs(s.power_w)
                for did in zone.producer_ids
                if (s := device_states.get(did)) is not None and s.power_w is not None
            )

            bat_w = 0.0
            bat_cost = 0.0
            for did in zone.storage_ids:
                s = device_states.get(did)
                if s is not None and s.power_w is not None and s.power_w < 0.0:
                    dw = abs(s.power_w)
                    bat_w += dw
                    bat_cost += dw * cost_bases.get(did, 0.0)

            total_w = pv_w + grid_w + bat_w
            if total_w <= 0.0:
                result[tariff_id] = import_price
                breakdown[tariff_id] = _parts(import_price, grid_w=1.0, pv_w=0.0, bat_w=0.0)
                continue

            bat_basis = bat_cost / bat_w if bat_w > 0.0 else 0.0
            price = (
                (pv_w  / total_w) * self._pv_opportunity_price
                + (grid_w / total_w) * import_price
                + (bat_w  / total_w) * bat_basis
            )
            result[tariff_id] = price
            breakdown[tariff_id] = _parts(price, grid_w=grid_w, pv_w=pv_w, bat_w=bat_w)

        # ── Pass 2: differential zones ────────────────────────────────────────
        for tariff_id, import_price in tariff_prices.items():
            if import_price <= 0.0 or tariff_id in result:
                continue
            zone = self._tariff_zones.get(tariff_id)
            if zone is None or zone.diff_minuend_id is None:
                # No zone or not a differential zone → flat rate
                result[tariff_id] = import_price
                breakdown[tariff_id] = _parts(import_price, grid_w=1.0, pv_w=0.0, bat_w=0.0)
                continue

            minuend_state = device_states.get(zone.diff_minuend_id)
            subtrahend_state = device_states.get(zone.diff_subtrahend_id) if zone.diff_subtrahend_id else None

            z1_w = (minuend_state.power_w or 0.0) if minuend_state else 0.0
            z2_w = (subtrahend_state.power_w or 0.0) if subtrahend_state else 0.0

            # Differential load (e.g. heatpump = Z1 - Z2), clamped to ≥ 0
            diff_w = max(0.0, z1_w - z2_w)
            if diff_w <= 0.0:
                result[tariff_id] = import_price
                breakdown[tariff_id] = _parts(import_price, grid_w=1.0, pv_w=0.0, bat_w=0.0)
                continue

            # Z2 exporting (negative) → that surplus can flow into this circuit,
            # but never more than the differential load itself.
            z2_feedback_w = min(diff_w, max(0.0, -z2_w))
            # Remainder is served by direct grid via Z1
            z1_grid_w = max(0.0, diff_w - z2_feedback_w)

            feedback_pv_w = z2_feedback_w
            feedback_bat_w = 0.0

            if z2_feedback_w > 0.0 and zone.diff_subtrahend_id:
                # The exported energy came from PV / batteries — NOT from the grid.
                # Compute the actual source cost of Z2's local generation, ignoring
                # the household import-tariff rate entirely.
                subtrahend_zone = next(
                    (z for z in self._tariff_zones.values()
                     if zone.diff_subtrahend_id in z.meter_ids),
                    None,
                )
                if subtrahend_zone is not None:
                    sz_pv_w = sum(
                        abs(s.power_w)
                        for did in subtrahend_zone.producer_ids
                        if (s := device_states.get(did)) is not None and s.power_w is not None
                    )
                    sz_bat_w = 0.0
                    sz_bat_cost = 0.0
                    for did in subtrahend_zone.storage_ids:
                        s = device_states.get(did)
                        if s is not None and s.power_w is not None and s.power_w < 0.0:
                            dw = abs(s.power_w)
                            sz_bat_w += dw
                            sz_bat_cost += dw * cost_bases.get(did, 0.0)
                    sz_source_w = sz_pv_w + sz_bat_w
                    if sz_source_w > 0.0:
                        sz_bat_basis = sz_bat_cost / sz_bat_w if sz_bat_w > 0.0 else 0.0
                        feedback_pv_w = z2_feedback_w * (sz_pv_w / sz_source_w)
                        feedback_bat_w = z2_feedback_w * (sz_bat_w / sz_source_w)
                        feedback_price = (
                            (sz_pv_w  / sz_source_w) * self._pv_opportunity_price
                            + (sz_bat_w / sz_source_w) * sz_bat_basis
                        )
                    else:
                        feedback_pv_w = z2_feedback_w
                        feedback_bat_w = 0.0
                        feedback_price = self._pv_opportunity_price
                else:
                    feedback_pv_w = z2_feedback_w
                    feedback_bat_w = 0.0
                    feedback_price = self._pv_opportunity_price
            else:
                feedback_price = import_price
                feedback_pv_w = 0.0
                feedback_bat_w = 0.0

            price = (
                (z1_grid_w     / diff_w) * import_price
                + (z2_feedback_w / diff_w) * feedback_price
            )
            result[tariff_id] = price
            breakdown[tariff_id] = _parts(
                price,
                grid_w=z1_grid_w,
                pv_w=feedback_pv_w,
                bat_w=feedback_bat_w,
            )

        return breakdown

    def _default_zone_grid_power_w(self, device_states: "dict[str, Any]") -> float | None:
        """Return grid import power (W) for the default tariff zone's sub-meter.

        In Messkonzept 8 this is the household meter (Z2, Tibber), which
        measures only the household circuit — not the heatpump circuit.
        Returns ``None`` when the default tariff zone has no meter devices.
        """
        default_tariff = self._cfg.default_tariff_id or ""
        zone = self._tariff_zones.get(default_tariff)
        if zone is None or not zone.meter_ids:
            return None
        vals = [
            s.power_w
            for did in zone.meter_ids
            if (s := device_states.get(did)) is not None and s.power_w is not None
        ]
        return sum(vals) if vals else None

    async def _fetch_tariff_prices(self, now: datetime) -> dict[str, float]:
        """Return current import price for every tariff that has one (> 0 €/kWh).

        Export-only tariffs (e.g. the ``grid`` feed-in tariff whose import
        price is 0.0) are skipped so the market-price cards only show
        metering zones where the import price is meaningful.

        Cached for 30 s: prices change hourly, but this is called for every
        /api/status request AND every ~3 s per SSE stream client, and each
        tariff lookup may be a remote ioBroker read.
        """
        cached = getattr(self, "_tariff_price_cache", None)
        if cached is not None and (time.monotonic() - cached[0]) < 30.0:
            return cached[1]

        prices: dict[str, float] = {}
        for tariff_id, tariff in self._tariffs.items():
            try:
                p = await tariff.price_at(now)
                if p > 0.0:
                    prices[tariff_id] = p
            except Exception:  # noqa: BLE001
                pass
        self._tariff_price_cache = (time.monotonic(), prices)
        return prices

    async def _build_tariff_weighted_price_forecast(self) -> tuple[list[ForecastPoint], dict[datetime, bool]]:
        """Return hourly import-price points weighted by per-tariff load share.

        The current MILP is single-node (one import variable), so we cannot yet
        optimise true multi-zone power flows. This helper provides a pragmatic
        approximation: blend tariff prices by forecast consumption per tariff.

        Example: if a timestep has 25% household load (Tibber) and 75% heatpump
        load (flat rate), the optimizer sees

            p_eff = 0.25 * p_household + 0.75 * p_heatpump

        which prevents over-valuing battery discharge during heatpump-dominated
        periods.
        """
        # Aggregate consumption forecasts by effective tariff.
        load_by_tariff: dict[str, dict[datetime, float]] = defaultdict(dict)
        all_timestamps: set[datetime] = set()

        for device_id, cfg in self._cfg.devices.items():
            device = self._registry.get(device_id)
            if device is None or device.role != DeviceRole.CONSUMER:
                continue
            fc_cfg = cfg.get("forecast")
            if not isinstance(fc_cfg, dict):
                continue

            tariff_id = cfg.get("tariff") or self._cfg.default_tariff_id
            if not tariff_id or tariff_id not in self._tariffs:
                continue

            try:
                provider = plugin_registry.build_forecast(
                    f"{device_id}_weighted_price_forecast",
                    fc_cfg,
                    self._build_ctx,
                )
                if provider is None or provider.quantity != ForecastQuantity.CONSUMPTION:
                    continue
                pts = await provider.get_forecast(self._horizon)
            except Exception as exc:  # noqa: BLE001
                _log.debug("Could not build forecast for %s: %s", device_id, exc)
                continue

            bucket = load_by_tariff[tariff_id]
            for pt in pts:
                ts = pt.timestamp
                bucket[ts] = bucket.get(ts, 0.0) + float(pt.value)
                all_timestamps.add(ts)

        if not all_timestamps:
            return [], {}

        # Load each tariff's schedule once — this is the authoritative source of
        # both prices AND the real data horizon.  ``price_at()`` returns 0.0 for
        # hours where the tariff has no data (e.g. Tibber tomorrow-prices not yet
        # published), which would create misleadingly low blended prices.  Loading
        # the schedule directly lets us (a) do a single API call per tariff and (b)
        # know exactly where real data ends so we can clip the timestamps.
        tariff_schedules: dict[str, list[TariffPoint]] = {}
        tariff_estimated_ts: dict[str, set[datetime]] = {}
        for tariff_id in load_by_tariff:
            tariff = self._tariffs.get(tariff_id)
            if tariff is None:
                continue
            try:
                sched = await tariff.price_schedule(self._horizon)
                if sched:
                    ext, est_ts = _extend_tariff_schedule_with_daily_repeat(sched, self._horizon)
                    tariff_schedules[tariff_id] = sorted(ext, key=lambda p: p.timestamp)
                    tariff_estimated_ts[tariff_id] = est_ts
            except Exception:  # noqa: BLE001
                pass

        if not tariff_schedules:
            return [], {}

        timestamps = sorted(all_timestamps)
        if not timestamps:
            return [], {}

        def _schedule_price_and_flag(
            sched: list[TariffPoint],
            estimated_ts: set[datetime],
            ts: datetime,
        ) -> tuple[float, bool]:
            """Nearest-neighbour price lookup + estimated-source flag."""
            best = min(sched, key=lambda p: abs((p.timestamp - ts).total_seconds()))
            return float(best.price_eur_per_kwh), (best.timestamp in estimated_ts)

        price_and_flag_by_tariff: dict[str, dict[datetime, tuple[float, bool]]] = {}
        for tariff_id, sched in tariff_schedules.items():
            est_set = tariff_estimated_ts.get(tariff_id, set())
            per_ts: dict[datetime, tuple[float, bool]] = {}
            for ts in timestamps:
                per_ts[ts] = _schedule_price_and_flag(sched, est_set, ts)
            price_and_flag_by_tariff[tariff_id] = per_ts

        weighted: list[ForecastPoint] = []
        weighted_estimated_by_ts: dict[datetime, bool] = {}
        for ts in timestamps:
            total_kw = 0.0
            weighted_sum = 0.0
            has_estimated_component = False
            for tariff_id, loads in load_by_tariff.items():
                load_kw = float(loads.get(ts, 0.0))
                if load_kw <= 0.0:
                    continue
                price, from_estimated = price_and_flag_by_tariff.get(tariff_id, {}).get(
                    ts, (0.0, False)
                )
                total_kw += load_kw
                weighted_sum += load_kw * price
                if from_estimated:
                    has_estimated_component = True

            if total_kw > 0.0:
                weighted.append(ForecastPoint(timestamp=ts, value=weighted_sum / total_kw))
                weighted_estimated_by_ts[ts] = has_estimated_component

        return weighted, weighted_estimated_by_ts

    def _pick_ui_variable_tariff_id(self) -> str | None:
        """Select a tariff ID suitable for variable-price charting.

        Prefer the configured default tariff if it is variable, otherwise the
        first configured variable tariff. Flat-rate tariffs are excluded.
        """

        def is_variable(tariff_cfg: dict[str, Any] | None) -> bool:
            if not isinstance(tariff_cfg, dict):
                return False
            return str(tariff_cfg.get("type") or "").strip().lower() != "flat_rate"

        default_id = self._cfg.default_tariff_id
        if default_id and is_variable(self._cfg.tariffs.get(default_id)):
            return default_id

        for tariff_id, tariff_cfg in self._cfg.tariffs.items():
            if is_variable(tariff_cfg):
                return tariff_id

        return None

    async def _build_ui_variable_price_forecast(self) -> tuple[list[ForecastPoint], dict[datetime, bool]]:
        """Return a variable-only import-price series for UI rendering.

        This keeps the plan chart aligned with user expectations (spot/variable
        tariff only) while the optimizer can still use a blended effective price.
        """
        tariff_id = self._pick_ui_variable_tariff_id()
        if not tariff_id:
            return [], {}

        tariff = self._tariffs.get(tariff_id)
        if tariff is None:
            return [], {}

        try:
            sched = await tariff.price_schedule(self._horizon)
            if not sched:
                return [], {}
            ext, est_ts = _extend_tariff_schedule_with_daily_repeat(sched, self._horizon)
            pts = [
                ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh)
                for tp in ext
            ]
            return pts, {tp.timestamp: (tp.timestamp in est_ts) for tp in ext}
        except Exception:  # noqa: BLE001
            return [], {}

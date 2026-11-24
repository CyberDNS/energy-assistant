"""Application — main orchestrator for the Energy Assistant platform.

Wires all platform components together and runs three concurrent async loops
(plus the fail-safe watchdog):

Polling loop (``poll_interval_s``, default 30 s)
    Reads current state from every registered device, persists it to the
    SQLite history store, and publishes ``DeviceStateEvent`` on the bus.
    After the very first tick it initialises the ``BatteryCostLedger`` from
    live SoC readings.

Planning loop (``plan_interval_s``, default 900 s)
    Runs on the slot grid, ``plan_lead_s`` (default 30 s) before a slot
    boundary, so each slot starts with a fresh plan (core/plan_schedule.py).
    Assembles an ``OptimizationContext`` from current device states, all
    forecast providers, and the tariff schedule, then runs the MILP
    optimizer and publishes a ``PlanUpdatedEvent``.  The ``ControlLoop``
    subscribes to this event and replaces its active plan immediately.

Control loop (``control_interval_s``, default 30 s)
    Builds a ``LiveSituation`` snapshot (grid power, current spot price,
    PV opportunity price, device states, elapsed dt) and calls
    ``ControlLoop.tick()``.  Each registered ``ControlContributor`` decides
    its desired setpoint; the loop sends commands and updates the ledger.

Fail-safe (core/failsafe.py)
    Every device is released to its own operation (``release()``) at start,
    reload and stop, while the plan is stale, and when a watchdog task sees
    no control tick for a while.

Usage (CLI)::

    python -m energy_assistant               # uses ./config.yaml
    python -m energy_assistant path/to/config.yaml

Usage (programmatic)::

    app = Application("config.yaml")
    await app.run_forever()          # blocks until SIGINT / SIGTERM
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..assets.ev import (
    _FEASIBILITY_MARGIN,
    EvChargerContributor,
    EvChargingAsset,
    EvChargingGoal,
    EvDayOverride,
    EvWeeklyTarget,
    default_weekly_target,
)
from ..assets.fleet import VehicleFleet
from ..assets.loader import (
    asset_zoneinfo,
    resolve_active_goals,
    upcoming_days,
)
from ..assets.threshold import ThresholdControlContributor
from ..config.yaml import YamlConfigLoader, fallback_config
from ..core.change_gate import ChangeGate
from ..core.composition import compose
from ..core.config import AppConfig
from ..core.control import (
    ControlLoop,
    LiveSituation,
    StorageControlContributor,
    surplus_opportunity_price,
)
from ..core.device_model import DeviceCategory, DeviceConfigError, DeviceSpec
from ..core.event import DeviceStateEvent, EventBus, PlanUpdatedEvent
from ..core.execution_log import DeviceTick, ExecutionLog
from ..core.failsafe import (
    FailSafe,
    default_control_stuck_after_s,
    default_plan_stale_after_s,
)
from ..core.forecast import ForecastProvider
from ..core.grid_limit import GridLimit
from ..core.ledger import BatteryCostLedger
from ..core.models import (
    DeviceState,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    Measurement,
    StorageConstraints,
    TariffPoint,
    ThresholdConstraints,
    ev_current_mode,
    ev_step_mode,
    intent_display_mode,
)
from ..core.optimizer import OptimizationContext, SecondaryTariffLoad
from ..core.plan_schedule import aligned_plan_period, next_plan_run
from ..core.plugin_registry import BuildContext, SharedPool
from ..core.price_curve import PriceCurveHistory, build_price_slots
from ..core.registry import DeviceRegistry
from ..core.savings import (
    DeviceLive,
    EvLive,
    LiveSample,
    SavingsSlot,
    SavingsState,
    SavingsTracker,
    day_ahead_window_end,
    fill_stored_values,
    reference_prices,
    stored_position,
)
from ..core.tariff import TariffModel
from ..core.topology import TopologyNode
from ..core.vehicles import MANUAL, ChargingSession, SessionChange
from ..loader.device_loader import (
    build,
    build_device_forecast_map,
    build_top_level_forecasts,
    make_build_context,
    validate_plugins,
)
from ..loader.energy_dashboard import import_energy_dashboard
from ..loader.forecast_config import effective_forecast_configs
from ..plugins import registry as plugin_registry
from ..plugins._homeassistant.client import HAClient
from ..plugins.milp_highs import MilpHigsOptimizer
from ..plugins.mqtt_publisher import (
    EvMqttPublisher,
    charging_mode_from_behaviour,
    select_discovery_devices,
)
from ..storage.sqlite import HISTORY_5M_RETENTION, SqliteStorageBackend
from ..version import version_info, version_label
from .config_api import mount_config_api
from .mcp_tools import mount_mcp_server
from .remote_access import DEFAULT_TRUSTED_HOSTS, ApiAccessMiddleware

_log = logging.getLogger(__name__)

# How often expired history rows are purged from SQLite.
_RETENTION_INTERVAL_S = 3600.0
# Time one device gets to go back to its own operation (fail-safe release).
_RELEASE_TIMEOUT_S = 20.0

# Shortest interval between two SoC pushes of one vehicle to its charger system.
_SOC_PUSH_MIN_INTERVAL_S = 60.0

# A persisted shadow-site state older than this is discarded on startup: the
# unplanned site then restarts level with the real one instead of carrying a
# battery level from before the downtime.
_SAVINGS_STATE_MAX_AGE = timedelta(hours=1)


def _problems_of(exc: Exception) -> list[str]:
    """The config problems an exception stands for (one line each)."""
    if isinstance(exc, DeviceConfigError):
        return list(exc.errors) or ["invalid configuration"]
    return [str(exc) or type(exc).__name__]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_utc(ts: datetime) -> datetime:
    """Aware UTC datetime; a naive one is read as UTC."""
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _savings_totals(rows: list[dict]) -> dict[str, Any]:
    """Sum savings slots (see ``SqliteStorageBackend.query_savings_slots``)."""
    real = sum(r["real_cost"] for r in rows)
    uncontrolled = sum(r["uncontrolled_cost"] for r in rows)
    components: dict[str, float] = {}
    for r in rows:
        for key, value in (r.get("components") or {}).items():
            components[key] = components.get(key, 0.0) + value
    return {
        "real": uncontrolled - real,
        "uncontrolled_cost": uncontrolled,
        "real_cost": real,
        "uncontrolled_bill": sum(r["uncontrolled_bill"] for r in rows),
        "real_bill": sum(r["real_bill"] for r in rows),
        "ev_saving": sum(r["ev_saving"] for r in rows),
        "components": components,
        "hours": sum(r["hours"] for r in rows),
    }


def _stored_positions(
    rows: list[dict], batteries: list[StorageConstraints]
) -> tuple[list[float | None], list[float | None]]:
    """Per slot: the reference price and the saving still stored in the real
    house's batteries beyond the uncontrolled one's (``stored_position``
    difference, € at the slot's end).  Slots recorded before the ledger
    values were stored use the nearest slot's cost per kWh; None only
    without any.  *rows*: every slot up to the window's end, oldest first."""
    by_id = {sc.device_id: sc for sc in batteries}
    prices = reference_prices([(r["slot_start"], r["hours"], r["import_price"]) for r in rows])
    details = fill_stored_values([r.get("stored_detail") for r in rows])
    positions: list[float | None] = []
    for detail, price in zip(details, prices):
        pos = stored_position(detail, by_id, price)
        positions.append(pos["difference"] if pos is not None else None)
    return prices, positions


def _stored_change(
    totals: dict[str, Any], first: int, last: int, positions: list[float | None]
) -> dict[str, Any]:
    """Add the change of the stored saving over slots *first*..*last* (indices
    into *positions*) to *totals*: ``stored_change`` and ``real_with_stored``
    (the saving counting energy still stored).  Before the first slot both
    houses start level (0 €)."""
    start = positions[first - 1] if first > 0 else 0.0
    end = positions[last] if last >= 0 else 0.0
    change = end - start if start is not None and end is not None else None
    return {
        **totals,
        "stored_position_start": start,
        "stored_position_end": end,
        "stored_change": change,
        "real_with_stored": totals["real"] + change if change is not None else None,
    }


_BUCKETS = ("hour", "day", "week")
_SLOT_DETAIL_MAX_HOURS = 72


def _bucket_start(ts: datetime, bucket: str) -> datetime:
    """Local start of the hour / day / week (Monday) *ts* falls into."""
    local = ts.astimezone()
    if bucket == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    day = local.date()
    if bucket == "week":
        day -= timedelta(days=day.weekday())
    # A naive datetime is read as local time (correct across DST changes).
    return datetime(day.year, day.month, day.day).astimezone()


@dataclasses.dataclass
class _TariffZone:
    """Devices assigned to a single tariff zone for market-price computation."""

    tariff_id: str
    meter_ids: list[str] = dataclasses.field(default_factory=list)
    """``meter`` devices that measure grid import for this circuit."""
    producer_ids: list[str] = dataclasses.field(default_factory=list)
    """``pv`` devices physically on this circuit."""
    storage_ids: list[str] = dataclasses.field(default_factory=list)
    """``battery`` devices physically on this circuit."""
    diff_minuend_id: str | None = None
    """For differential zones (e.g. heatpump = Z1 − Z2): the Z1 (minuend) device."""
    diff_subtrahend_id: str | None = None
    """For differential zones: the Z2 (subtrahend) device whose zone's market price
    is used for the feedback contribution when Z2 is exporting."""



# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _learning_category(item: dict[str, Any]) -> str:
    """Learning-tab category of one ``/api/learned`` entry, one per learning
    method: ``consumption`` (daily energy of a consumer, regressed on
    temperature and day type, times an hourly shape), ``pv`` (the PV system
    learned from its production against past weather forecasts),
    ``thermal`` (devices kept between a min and a max: aquarium, fridge, AC)
    and ``storage`` (battery round-trip efficiency from closed
    charge/discharge cycles)."""
    return {"thermal": "thermal", "efficiency": "storage", "pv": "pv"}.get(
        item.get("kind", ""), "consumption"
    )


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


#: Window after the planning horizon whose PV surplus counts as refill.
_REFILL_WINDOW = timedelta(hours=24)


def _step_value(points: list[ForecastPoint], ts: datetime) -> float | None:
    """Value of a step series (each point holds until the next) at *ts*."""
    value = None
    for p in points:
        if p.timestamp > ts:
            break
        value = p.value
    return value


def _battery_refill_kwh(
    pv: list[ForecastPoint],
    consumption: list[ForecastPoint],
    start: datetime,
    batteries: list[StorageConstraints],
    window: timedelta = _REFILL_WINDOW,
    step: timedelta = timedelta(minutes=15),
) -> dict[str, float]:
    """Energy (kWh stored) each battery can take in from PV surplus in
    ``[start, start + window)``: per step the surplus (PV − consumption)
    is shared by charge power, capped at each battery's charge power and
    stored with its charge efficiency.  Empty when the PV forecast does not
    reach the end of the window (unknown, not zero)."""
    pv = sorted(pv, key=lambda p: p.timestamp)
    consumption = sorted(consumption, key=lambda p: p.timestamp)
    end = start + window
    if not pv or not batteries or pv[-1].timestamp < end - timedelta(hours=1):
        return {}
    total_charge = sum(sc.max_charge_kw for sc in batteries)
    if total_charge <= 0:
        return {}
    h = step.total_seconds() / 3600.0
    refill = {sc.device_id: 0.0 for sc in batteries}
    t = start
    while t < end:
        surplus = (_step_value(pv, t) or 0.0) - (_step_value(consumption, t) or 0.0)
        if surplus > 0:
            for sc in batteries:
                share = surplus * sc.max_charge_kw / total_charge
                refill[sc.device_id] += min(share, sc.max_charge_kw) * h * sc.charge_efficiency
        t += step
    return {b: round(v, 3) for b, v in refill.items()}


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


async def _virtual_forecast_power_w(spec: DeviceSpec | None, provider: Any) -> float | None:
    """Return forecast power in watts for virtual generic consumers.

    *provider* is the device's forecast provider (static profile, learned …).
    """
    if spec is None or spec.type != "generic_consumer" or provider is None:
        return None
    try:
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


class WeeklyPlanRow(BaseModel):
    weekday: int              # ISO 1=Mon … 7=Sun
    enabled: bool
    target_soc_pct: float
    target_by: str            # "HH:MM" asset-local


class WeeklyPlanBody(BaseModel):
    weekly: list[WeeklyPlanRow]


class DayOverrideBody(BaseModel):
    skip: bool = False
    target_soc_pct: float | None = None
    target_by: str | None = None   # "HH:MM" asset-local


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
        self._plan_lead_s: float
        self._poll_interval_s: float
        self._control_interval_s: float
        self._history_retention: timedelta
        self._execution_log: ExecutionLog | None = None
        self._day_ahead_flagged: date | None = None
        self._dry_run: bool
        self._dry_run_log_gate = ChangeGate()
        self._first_poll_done: asyncio.Event
        self._api: FastAPI
        # One asset per vehicle, kept in place (other components hold the
        # list); device_id = the charger it is at, "" when away.
        self._ev_assets: list[EvChargingAsset] = []
        self._ev_contributors: list[EvChargerContributor] = []
        self._fleet: VehicleFleet
        # Last SoC pushed to a charger system per vehicle, and when.
        self._soc_pushed: dict[str, tuple[float, Any, float]] = {}
        # Last refresh-button press per vehicle (monotonic seconds).
        self._refresh_pressed: dict[str, float] = {}
        self._threshold_constraints: list[ThresholdConstraints] = []
        self._thermal_learners: dict[str, Any] = {}
        self._efficiency_learners: dict[str, Any] = {}
        self._threshold_contributors: list[ThresholdControlContributor] = []
        self._ev_weekly_plans: dict[str, dict[int, EvWeeklyTarget]] = {}
        self._ev_day_overrides: dict[str, dict[date, EvDayOverride]] = {}
        self._ev_force_charge: dict[str, float] = {}
        # When each car was plugged in (None = unplugged or not yet seen).
        # A missed deadline is only caught up at full power if the car was
        # plugged in when it passed and has stayed plugged in since — see
        # resolve_active_goals. Set on the first plugged observation (so a
        # restart counts as a fresh plug-in) and cleared on unplug.
        self._ev_plugged_since: dict[str, datetime | None] = {}
        # Local date per asset whose target was already met today — written
        # by resolve_active_goals so SoC dropping afterwards (the car being
        # driven) doesn't resurrect that deadline as overdue.
        self._ev_overdue_dismissed: dict[str, date] = {}
        # In-memory staging for the HA/MQTT date+time+soc picker entities.
        self._staged_overrides: dict[str, tuple[float, datetime]] = {}
        self._disabled_chargepoints: set[str] = set()
        self._last_ev_goals: list[EvChargingGoal] = []
        self._mqtt_publisher: EvMqttPublisher | None = None
        # >0 while _run_plan() is executing — surfaced in the status payload
        # so the UI can show "recalculating" after plan-affecting edits.
        self._plan_active_count = 0
        # Bumped whenever a new plan is published (SSE clients watch it).
        self._plan_seq = 0
        self._display_plan: EnergyPlan | None = None
        # Config reload at runtime (reload_config).
        self._config_text = ""
        self._reload_lock = asyncio.Lock()
        self._reloading = False
        self._runtime_tasks: list[asyncio.Task[None]] = []
        self._service_tasks: list[asyncio.Task[None]] = []
        # Problems of the config file.  While there are any, the app runs in
        # "configuration needed" state: the UI, the config API and the Home
        # Assistant connection run on a fallback configuration
        # (config/yaml.fallback_config), nothing is planned or controlled.
        self._config_problems: list[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build all components and launch the async loops."""
        _log.info("Energy Assistant starting  config=%s  db=%s",
                  self._config_path, self._db_path)
        cfg = self._load_config()

        # Persistent storage — stays open across config reloads.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._storage = SqliteStorageBackend(self._db_path)
        await self._storage.start()

        try:
            await self._build_runtime(cfg)
        except Exception as exc:  # a config problem must not stop the process
            if self._config_problems:
                raise  # the fallback itself failed: a bug, not a config mistake
            _log.exception("Could not build the configuration")
            await self._enter_config_needed(_problems_of(exc))

        # API + loops.  The API and the history maintenance run for the whole
        # process lifetime; the other loops belong to the runtime and are
        # restarted by reload_config().
        self._api = self._build_api()
        port = int(os.environ.get("ENERGY_ASSISTANT_PORT", "") or self._cfg.server.get("port", 8088))
        _log.info("API listening on http://0.0.0.0:%d", port)
        _log.info("Web UI available at http://localhost:%d", port)
        self._service_tasks = [
            asyncio.create_task(self._api_task(port), name="api"),
            asyncio.create_task(self._retention_loop(), name="retention"),
        ]
        self._start_loops()
        _log.info("All loops started")

    def _load_config(self) -> AppConfig:
        """Read and parse the config file; remembers its text (``_config_text``).

        A file with problems gives the fallback configuration and puts the
        app in "configuration needed" state (``config_problems``)."""
        text = self._config_path.read_text() if self._config_path.exists() else ""
        self._config_text = text
        try:
            cfg = YamlConfigLoader(self._config_path).load_text(text)
            validate_plugins(cfg.devices, cfg.tariffs)
        except Exception as exc:  # noqa: BLE001 — never stop on a config mistake
            self._set_config_problems(_problems_of(exc))
            return fallback_config(text)
        self._config_problems = []
        return cfg

    def _set_config_problems(self, problems: list[str]) -> None:
        if problems != self._config_problems:
            _log.error(
                "Configuration needed — nothing is planned or controlled until "
                "config.yaml is fixed (in the web UI under Settings):\n  - %s",
                "\n  - ".join(problems),
            )
        self._config_problems = problems

    async def _enter_config_needed(self, problems: list[str]) -> None:
        """Run the fallback configuration (after building the real one failed)."""
        self._set_config_problems(problems)
        await self._close_devices_quietly()
        await self._build_runtime(fallback_config(self._config_text),
                                  getattr(self, "_build_ctx", None))

    async def _close_devices_quietly(self) -> None:
        if hasattr(self, "_registry"):
            await self._close_devices()

    @property
    def config_problems(self) -> list[str]:
        """Problems of the config file; empty when it is running."""
        return list(self._config_problems)

    def configuration_state(self) -> dict[str, Any]:
        """``running`` or ``configuration_needed``, with the problems, and
        which Home Assistant the app is connected to (for the UI)."""
        ha = self._cfg.backends.homeassistant if hasattr(self, "_cfg") else None
        return {
            "state": "configuration_needed" if self._config_problems else "running",
            "problems": list(self._config_problems),
            "devices": len(self._cfg.devices) if hasattr(self, "_cfg") else 0,
            "homeassistant": (
                None if ha is None else
                {"source": "supervisor", "label": "Home Assistant (this installation)"}
                if ha.supervisor else {"source": "config", "label": ha.url}
            ),
        }

    def _start_loops(self) -> None:
        if self._config_problems:
            # Configuration needed: nothing is planned or controlled.
            self._runtime_tasks = []
            self.tasks = list(self._service_tasks)
            return
        self._runtime_tasks = [
            asyncio.create_task(self._polling_loop(), name="polling"),
            asyncio.create_task(self._planning_loop(), name="planning"),
            asyncio.create_task(self._control_task(), name="control"),
            asyncio.create_task(self._failsafe.watch(), name="failsafe"),
        ]
        self.tasks = [*self._runtime_tasks, *self._service_tasks]

    async def _build_runtime(self, cfg: AppConfig, previous: BuildContext | None = None) -> None:
        """Build everything that depends on the configuration.

        Called at startup and again by ``reload_config``; *previous* is then
        the build context of the runtime being replaced, whose connections
        are reused where the configuration still asks for them.
        """
        # 1 — Config
        self._cfg = cfg
        log_level = str(os.environ.get("LOG_LEVEL") or self._cfg.logging.get("level", "INFO")).upper()
        logging.getLogger().setLevel(log_level)
        # httpx logs one INFO line per HTTP request (every device poll) —
        # that's query-noise, not app-level info, so only surface it once
        # the app itself is turned down to DEBUG.
        if log_level != "DEBUG":
            logging.getLogger("httpx").setLevel(logging.WARNING)
            # Same for the MCP endpoint: one line per stateless tool call.
            logging.getLogger("mcp").setLevel(logging.WARNING)
        opt = self._cfg.optimizer
        ctl = self._cfg.controller
        self._plan_interval_s = float(ctl.get("plan_interval_s", 900))
        self._plan_lead_s = float(ctl.get("plan_lead_s", 30))
        self._control_interval_s = float(ctl.get("control_interval_s", 30))
        self._poll_interval_s = float(ctl.get("poll_interval_s", self._control_interval_s))
        self._dry_run = bool(ctl.get("dry_run", False)) or os.environ.get("ENERGY_ASSISTANT_DRY_RUN", "") == "1"
        # Safety buffer for the EV missed-deadline feasibility check — see
        # build_goal_from_parts. 1.2 = require 20% more time than the bare
        # minimum before declaring a deadline still reachable.
        self._ev_feasibility_margin = float(ctl.get("ev_feasibility_margin", _FEASIBILITY_MARGIN))
        # Measurements / ledger history older than this are purged.  The
        # history API serves at most 7 days, so keep one extra day of slack.
        self._history_retention = timedelta(
            days=float(self._cfg.server.get("history_retention_days", 8))
        )
        horizon_h = int(opt.get("horizon_hours", 24))
        self._horizon = timedelta(hours=horizon_h)

        # 2 — Build devices / tariffs / topology (shared connection pool)
        SharedPool.begin_build()
        ctx = make_build_context(self._cfg, previous)
        if ctx.modbus_pool is not None:
            ctx.modbus_pool.begin_build()
        # Plugins that keep state across restarts (fitted forecast models)
        # store it next to the database.
        ctx.data_dir = self._db_path.parent
        self._build_ctx = ctx
        # Consumers of the HA Energy dashboard (opt-in) join the configured
        # devices before anything is built from them.
        await import_energy_dashboard(self._cfg, ctx.ha_client, ctx.data_dir)
        self._registry, self._tariffs, self._topology = build(self._cfg, ctx=ctx)
        _log.info("Loaded %d devices, %d tariffs", len(self._registry), len(self._tariffs))

        # 3 — Forecast providers (top-level + per-device, same ctx)
        # Per-device providers are kept by device id as well, so other code
        # paths reuse the same (possibly stateful) instance.
        self._device_forecast_providers = build_device_forecast_map(self._cfg, ctx=ctx)
        self._forecast_providers = build_top_level_forecasts(self._cfg, ctx=ctx) + list(
            self._device_forecast_providers.values()
        )
        _log.info("Loaded %d forecast providers", len(self._forecast_providers))

        # 5 — Event bus
        self._bus = EventBus()

        # 6 — Storage constraints + optimizer
        # Composition: optimizer / control inputs from the device list.
        composition = compose(self._cfg.devices)
        self._storage_constraints = composition.storage_constraints
        self._threshold_constraints = composition.threshold_constraints
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
        self._control_loop = ControlLoop(
            ledger=self._ledger, grid_limit=self._grid_limit()
        )
        # Releases every device when the plan goes stale or the control loop
        # hangs (core/failsafe.py).
        self._failsafe = FailSafe(
            self._release_devices,
            plan_stale_after_s=(
                float(ctl["plan_stale_after_min"]) * 60.0
                if ctl.get("plan_stale_after_min") is not None
                else default_plan_stale_after_s(self._plan_interval_s)
            ),
            control_stuck_after_s=(
                float(ctl["control_stuck_after_min"]) * 60.0
                if ctl.get("control_stuck_after_min") is not None
                else default_control_stuck_after_s(self._control_interval_s)
            ),
        )
        for sc in self._storage_constraints:
            self._control_loop.register_contributor(StorageControlContributor(sc))
        _log.info("Registered %d storage contributors", len(self._storage_constraints))

        # 7b — Vehicles, chargers and their sessions (assets/fleet.py)
        self._fleet = VehicleFleet(self._cfg.devices)
        self._fleet.sessions.restore(
            ChargingSession(
                charger_id=row["charger_id"], vehicle_id=row["vehicle_id"],
                started_at=row["plugged_at"], source=row["source"], session_id=row["id"],
            )
            for row in await self._storage.load_open_vehicle_sessions()
        )
        for charger_id, choice in (await self._storage.load_next_vehicles()).items():
            try:
                self._fleet.sessions.set_next(charger_id, choice)
            except ValueError as exc:
                _log.info("Dropping stored next vehicle for %s: %s", charger_id, exc)
        self._fleet.sessions.guest_modes.update(await self._storage.load_guest_modes())
        self._fleet.sessions.prune_guest_modes()
        await self._report_orphaned_data()
        self._ev_contributors = list(self._fleet.contributors.values())
        for contrib in self._ev_contributors:
            self._control_loop.register_contributor(contrib)
        # Load persisted charge plans, overrides, force-charge and disabled state
        self._ev_weekly_plans = await self._storage.load_all_ev_weekly_plans()
        self._ev_day_overrides = await self._storage.load_all_ev_day_overrides()
        self._ev_force_charge = await self._storage.load_all_ev_force_charge()
        self._disabled_chargepoints = await self._storage.load_all_ev_disabled()
        self._apply_sessions()
        _log.info(
            "Loaded %d vehicles, %d chargers (%d weekly plans, %d day overrides, %d forced, "
            "%d disabled, %d open sessions)",
            len(self._fleet.vehicles), len(self._fleet.chargers), len(self._ev_weekly_plans),
            sum(len(v) for v in self._ev_day_overrides.values()),
            len(self._ev_force_charge), len(self._disabled_chargepoints),
            len(self._fleet.sessions.sessions),
        )

        # 7b' — Savings accounting: the real house vs. a simulated
        # uncontrolled house (core/savings.py).
        self._savings = SavingsTracker(
            self._storage_constraints,
            step_minutes=self._optimizer._step_min,
            state=await self._load_savings_state(),
            devices=self._threshold_constraints,
        )
        # Plan fidelity: what the control loop did with each slot's intent.
        self._execution_log = ExecutionLog(step_minutes=self._optimizer._step_min)

        # 7c — Thermal loads (threshold control) + contributors
        self._threshold_contributors = [
            ThresholdControlContributor(tc) for tc in self._threshold_constraints
        ]
        for contrib in self._threshold_contributors:
            self._control_loop.register_contributor(contrib)
        _log.info("Loaded %d thermal loads", len(self._threshold_constraints))
        # Learned thermal model per threshold device (plugins/learned/thermal).
        from ..plugins.learned.thermal import build_thermal_learners  # noqa: PLC0415
        self._thermal_learners = build_thermal_learners(
            self._threshold_constraints, self._cfg.devices,
            self._build_ctx.ha_client, self._build_ctx.data_dir,
        )
        if self._thermal_learners:
            _log.info("Learning thermal models for %s", ", ".join(self._thermal_learners))
        # Learned round-trip efficiency per battery (plugins/learned/efficiency).
        from ..plugins.learned.efficiency import build_efficiency_learners  # noqa: PLC0415
        self._efficiency_learners = build_efficiency_learners(
            self._storage_constraints, self._cfg.devices, self._storage,
            self._build_ctx.data_dir,
        )
        self._savings.set_batteries(self._planning_storage_constraints())

        # 7d — MQTT publisher (optional — only started when mqtt: is configured)
        if self._cfg.backends.mqtt:
            self._mqtt_publisher = EvMqttPublisher(
                cfg=self._cfg.backends.mqtt,
                assets=self._ev_assets,
                on_stage=self._mqtt_stage_target,
                on_enable_override=self._mqtt_enable_override,
                on_disable_override=self._mqtt_disable_override,
                on_set_weekly=self._mqtt_set_weekly,
                # A dry run controls nothing, so it has no savings to report —
                # and a test instance must not overwrite the real totals.
                savings_kpis=not self._dry_run,
                # Every device not already fed from HA, as a sub-device.
                devices=select_discovery_devices(
                    self._cfg.devices,
                    {d.device_id for d in self._registry.all()},
                ),
                # A dry run must not remove the real instance's entities.
                remove_stale_vehicles=not self._dry_run,
                chargers=[(cid, spec.display_name) for cid, spec in self._fleet.chargers.items()],
                on_select_charger=self.select_charger,
                on_set_guest_mode=lambda mode: self.set_guest_mode(None, mode),
            )
            await self._mqtt_publisher.publish_weekly_plans(self._ev_weekly_plans)
            await self._publish_savings()
            await self._mqtt_publisher.start()
            _log.info("MQTT publisher started")

        # 8 — Subscribe control loop to plan updates via event bus
        # _plan_seq lets the /api/stream SSE endpoint notify UI clients the
        # moment a new plan is published.  _display_plan is what the API
        # serves — it may briefly be the coarse startup fast-pass plan, which
        # is DISPLAY-ONLY and never reaches the control loop: a throwaway
        # plan must not actuate hardware (a spurious 'run' slot switched the
        # aquarium cooler on at startup, and the compressor min-runtime then
        # held it on for 30 min against the refined plan).
        self._display_plan = None

        async def _on_plan_updated(event: PlanUpdatedEvent) -> None:
            self._control_loop.update_plan(event.plan)
            self._display_plan = event.plan
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
            (d.device_id for d in self._registry.by_category(DeviceCategory.PV)),
            None,
        )
        self._pv_opportunity_price = 0.0  # refreshed on first planning cycle
        if not hasattr(self, "_price_history"):
            self._price_history = PriceCurveHistory()
        self._tariff_zones = self._build_tariff_zones()
        _log.info(
            "Tariff zones: %s",
            {zid: (len(z.meter_ids), len(z.producer_ids), len(z.storage_ids))
             for zid, z in self._tariff_zones.items()},
        )
        self._first_poll_done = asyncio.Event()

        if self._dry_run:
            _log.warning("DRY RUN — control commands will be logged but not sent")

        # 10 — Connections the new configuration no longer uses
        await SharedPool.sweep()
        if ctx.modbus_pool is not None:
            await ctx.modbus_pool.sweep()
        if previous is not None and previous.ha_client not in (None, ctx.ha_client):
            await previous.ha_client.close()

    async def stop(self) -> None:
        """Cancel all running tasks and close the storage backend."""
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self._release_devices()
        if self._mqtt_publisher is not None:
            await self._mqtt_publisher.stop()
        if hasattr(self, "_storage"):
            await self._storage.stop()
        ha_client = getattr(getattr(self, "_build_ctx", None), "ha_client", None)
        if ha_client is not None:
            await ha_client.close()
        _log.info("Energy Assistant stopped")

    async def run_forever(self) -> None:
        """Start and block until a task ends (e.g. cancelled by SIGINT)."""
        await self.start()
        try:
            while True:
                tasks = list(self.tasks)
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if self._reloading or tasks != self.tasks:
                    # reload_config() replaced the runtime loops.
                    async with self._reload_lock:
                        continue
                for task in tasks:
                    if task.done():
                        task.result()  # a loop must never end: raise what ended it
                break
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Config reload at runtime
    # ------------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def loaded_config_text(self) -> str:
        """The config file content the running configuration was built from."""
        return self._config_text

    def running_devices(self) -> dict[str, DeviceSpec]:
        """The device specs of the running configuration (incl. imported ones)."""
        return self._cfg.devices

    def ha_client(self) -> HAClient | None:
        return self._build_ctx.ha_client

    async def check_config_text(self, text: str) -> AppConfig:
        """Parse and fully validate *text* as config file content, exactly as
        a start with it would.  Raises ``DeviceConfigError`` (or another
        exception for problems outside ``devices:``); changes nothing."""
        cfg = YamlConfigLoader(self._config_path).load_text(text)
        await import_energy_dashboard(cfg, self._build_ctx.ha_client, self._build_ctx.data_dir)
        validate_plugins(cfg.devices, cfg.tariffs)
        return cfg

    async def reload_config(self) -> None:
        """Apply the config file without restarting the process.

        The file is validated first; an invalid file changes nothing.  Then
        the polling, planning and control loops are stopped, every component
        built from the configuration is rebuilt (connections that are still
        needed stay open) and the loops start again — the same sequence as a
        restart, in a few seconds, with the API and the database staying up.
        Stored data (plans, sessions, learned models, savings) is keyed by
        device id and is picked up again.

        Not applied by a reload: ``server.port``, ``server.api_token`` /
        ``trusted_hosts`` — they need a restart.
        """
        async with self._reload_lock:
            text = self._config_path.read_text() if self._config_path.exists() else ""
            was_broken = bool(self._config_problems)
            try:
                await self.check_config_text(text)
                cfg = YamlConfigLoader(self._config_path).load_text(text)
            except Exception as exc:
                if was_broken:
                    # Nothing runs that the file could break: just show the
                    # problems of the file as it is now.
                    self._config_text = text
                    self._set_config_problems(_problems_of(exc))
                raise
            previous_text, previous_ctx = self._config_text, self._build_ctx
            self._reloading = True
            try:
                await self._stop_runtime()
                try:
                    self._config_problems = []
                    await self._build_runtime(cfg, previous_ctx)
                    self._config_text = text
                except Exception as exc:
                    _log.exception("Config reload failed — going back to the previous configuration")
                    await self._close_devices()
                    if was_broken:
                        self._config_text = text
                        await self._enter_config_needed(_problems_of(exc))
                    else:
                        await self._build_runtime(
                            YamlConfigLoader(self._config_path).load_text(previous_text), previous_ctx
                        )
                    raise
                finally:
                    self._start_loops()
            finally:
                self._reloading = False
            if was_broken:
                _log.info("Configuration fixed — planning and control start")
            _log.info("Configuration reloaded: %d devices", len(self._registry))

    async def _stop_runtime(self) -> None:
        """Stop the loops and everything built from the configuration."""
        for task in self._runtime_tasks:
            task.cancel()
        await asyncio.gather(*self._runtime_tasks, return_exceptions=True)
        # Let a plan run that is in flight finish on the old runtime.
        deadline = time.monotonic() + 120.0
        while self._plan_active_count and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        # The fallback configuration (configuration needed) tracks nothing,
        # so it must not overwrite the stored savings state.
        if not self._config_problems:
            try:
                await self._storage.save_savings_state(self._savings.state.to_dict())
            except Exception as exc:  # noqa: BLE001
                _log.warning("Could not save the savings state before the reload: %s", exc)
        if self._mqtt_publisher is not None:
            await self._mqtt_publisher.stop()
            self._mqtt_publisher = None
        await self._release_devices()
        await self._close_devices()

    async def _release_devices(self) -> None:
        """Hand every controlled device back to its own operation (each
        device's ``release()``) — at start, reload and stop, and when the
        fail-safe sees a stale plan or a stuck control loop, so a forced
        charge, a stopped wallbox or a switched-off cooler never outlives the
        control that commanded it.  Each device gets ``_RELEASE_TIMEOUT_S``;
        one that hangs or fails does not keep the others from being
        released.  Not in dry-run."""
        if getattr(self, "_dry_run", True) or not hasattr(self, "_registry"):
            return

        async def release_one(device: Any) -> None:
            release = getattr(device, "release", None)
            if release is None:
                return
            try:
                await asyncio.wait_for(release(), _RELEASE_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _log.warning("Could not release device %r: no answer within %.0f s",
                             device.device_id, _RELEASE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                _log.warning("Could not release device %r: %s", device.device_id, exc)

        await asyncio.gather(*(release_one(d) for d in self._registry.all()))

    async def _close_devices(self) -> None:
        """Stop the background work of devices that own some (pollers)."""
        for device in self._registry.all():
            close = getattr(device, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as exc:  # noqa: BLE001
                _log.warning("Could not close device %r: %s", device.device_id, exc)

    # ------------------------------------------------------------------
    # EV charge-plan helpers
    # ------------------------------------------------------------------

    def _ev_asset(self, asset_id: str) -> EvChargingAsset | None:
        return next((a for a in self._ev_assets if a.asset_id == asset_id), None)

    async def _save_ev_weekly_plan(self, asset_id: str, entries: list[EvWeeklyTarget]) -> None:
        """Persist the weekly plan, mirror it to MQTT and trigger a re-plan."""
        await self._storage.set_ev_weekly_plan(asset_id, entries)
        self._ev_weekly_plans[asset_id] = {e.weekday: e for e in entries}
        _log.info("EV weekly plan saved: %r (%d enabled days)",
                  asset_id, sum(e.enabled for e in entries))
        if self._mqtt_publisher is not None:
            await self._mqtt_publisher.publish_weekly_plans(self._ev_weekly_plans)
        asyncio.create_task(self._run_plan())

    async def _set_ev_day_override(self, asset_id: str, override: EvDayOverride) -> None:
        await self._storage.set_ev_day_override(asset_id, override)
        self._ev_day_overrides.setdefault(asset_id, {})[override.date] = override

    async def _clear_ev_day_override(self, asset_id: str, day: date) -> None:
        await self._storage.clear_ev_day_override(asset_id, day)
        self._ev_day_overrides.get(asset_id, {}).pop(day, None)

    async def _purge_expired_ev_overrides(self) -> None:
        """Drop overrides whose local calendar day has passed (asset timezone)."""
        now = datetime.now(timezone.utc)
        for asset in self._ev_assets:
            today_local = now.astimezone(asset_zoneinfo(asset)).date()
            overrides = self._ev_day_overrides.get(asset.asset_id)
            if not overrides:
                continue
            expired = [d for d in overrides if d < today_local]
            if expired:
                await self._storage.purge_ev_day_overrides_before(asset.asset_id, today_local)
                for d in expired:
                    overrides.pop(d, None)
                _log.info("EV overrides expired for %r: %s", asset.asset_id, expired)

    async def _apply_force_charge(self, asset_id: str, target_soc_pct: float) -> None:
        await self._storage.set_ev_force_charge(asset_id, target_soc_pct)
        self._ev_force_charge[asset_id] = target_soc_pct
        asset = self._ev_asset(asset_id)
        self._fleet.apply(self._disabled_chargepoints, self._ev_force_charge)
        # Write the chosen target as the instant-charging SoC limit so the
        # wallbox itself stops at the target between our control ticks.
        if asset is not None:
            device = self._registry.get(asset.device_id)
            if device is not None and hasattr(device, "update_target_soc"):
                device.update_target_soc(target_soc_pct)
        _log.info("EV force charge ON: %r → %.0f%%", asset_id, target_soc_pct)
        asyncio.create_task(self._run_plan())

    async def _clear_force_charge(self, asset_id: str, reason: str) -> None:
        await self._storage.clear_ev_force_charge(asset_id)
        self._ev_force_charge.pop(asset_id, None)
        self._fleet.apply(self._disabled_chargepoints, self._ev_force_charge)
        _log.info("EV force charge OFF: %r (%s)", asset_id, reason)
        asyncio.create_task(self._run_plan())

    # ------------------------------------------------------------------
    # MQTT callbacks (called by EvMqttPublisher on incoming commands)
    # ------------------------------------------------------------------
    # The HA entities stage a (soc, deadline) pair; the override switch maps
    # it onto a dated override for the deadline's local calendar date.

    def _staged_override_parts(self, asset_id: str) -> tuple[float, date, str] | None:
        staged = self._staged_overrides.get(asset_id)
        asset = self._ev_asset(asset_id)
        if staged is None or asset is None:
            return None
        soc, deadline_utc = staged
        local = deadline_utc.astimezone(asset_zoneinfo(asset))
        return soc, local.date(), local.strftime("%H:%M")

    def _mqtt_override_active(self, asset_id: str) -> bool:
        parts = self._staged_override_parts(asset_id)
        if parts is None:
            return False
        _soc, day, _hhmm = parts
        ov = self._ev_day_overrides.get(asset_id, {}).get(day)
        return ov is not None and not ov.skip

    async def _mqtt_stage_target(self, asset_id: str, soc: float, deadline: datetime) -> None:
        was_active = self._mqtt_override_active(asset_id)
        self._staged_overrides[asset_id] = (soc, deadline)
        _log.info("MQTT staged: %r → %.0f%% by %s", asset_id, soc, deadline)
        # If the override switch is ON, apply the updated staged values immediately
        if was_active:
            await self._mqtt_enable_override(asset_id)

    async def _mqtt_enable_override(self, asset_id: str) -> None:
        parts = self._staged_override_parts(asset_id)
        if parts is None:
            _log.warning("MQTT enable_override: no staged values for %r", asset_id)
            return
        soc, day, hhmm = parts
        await self._set_ev_day_override(
            asset_id, EvDayOverride(date=day, skip=False, target_soc_pct=soc, target_by=hhmm)
        )
        _log.info("MQTT override enabled: %r → %.0f%% on %s at %s", asset_id, soc, day, hhmm)
        asyncio.create_task(self._run_plan())

    async def _mqtt_set_weekly(self, asset_id: str, weekday: int, changes: dict[str, Any]) -> None:
        """Change one field of one weekday's target (HA weekly-plan entities)."""
        asset = self._ev_asset(asset_id)
        if asset is None:
            return
        rows = dict(self._ev_weekly_plans.get(asset_id, {}))
        rows[weekday] = dataclasses.replace(default_weekly_target(asset, rows, weekday), **changes)
        await self._save_ev_weekly_plan(asset_id, [rows[wd] for wd in sorted(rows)])

    async def _mqtt_disable_override(self, asset_id: str) -> None:
        parts = self._staged_override_parts(asset_id)
        if parts is None:
            return
        _soc, day, _hhmm = parts
        if day in self._ev_day_overrides.get(asset_id, {}):
            await self._clear_ev_day_override(asset_id, day)
            _log.info("MQTT override disabled: %r for %s (staged values kept)", asset_id, day)
            asyncio.create_task(self._run_plan())

    # ------------------------------------------------------------------
    # History retention
    # ------------------------------------------------------------------

    async def _purge_history(self) -> None:
        """Delete ledger history and diagnostics older than the retention,
        roll the device history up and drop its expired 5-minute rows."""
        cutoff = datetime.now(timezone.utc) - self._history_retention
        deleted = await self._storage.purge_before(cutoff)
        deleted += await self._storage.purge_savings(datetime.now(timezone.utc))
        deleted += await self._storage.maintain_history()
        if deleted:
            _log.info("Purged %d history rows older than %s", deleted, cutoff.isoformat())

    async def _retention_loop(self) -> None:
        """Purge expired history now and then once per hour.

        Runs in the background rather than in start(): on a large legacy DB
        the first purge takes minutes, and blocking startup that long got the
        add-on killed (and the work rolled back) before it ever finished.
        The first pass also performs the one-time migration — dropping the
        redundant index and the VACUUM — once the purge has shrunk the data,
        so both are quick.
        """
        first = True
        while True:
            try:
                await self._purge_history()
                if first:
                    # Databases from before the 5-minute / hourly history:
                    # every reading was stored; roll them up and drop them.
                    migrated = await self._storage.migrate_raw_measurements()
                    if migrated:
                        _log.info("Rolled %d raw measurements up into the compact history", migrated)
                    await self._storage.drop_redundant_index()
                    if await self._storage.enable_incremental_vacuum():
                        _log.info("Compacted history DB (one-time VACUUM)")
                    first = False
            except Exception as exc:  # noqa: BLE001
                _log.warning("History maintenance failed: %s", exc)
            await asyncio.sleep(_RETENTION_INTERVAL_S)

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
                    states[device.device_id] = await device.get_state()
                except Exception as exc:  # noqa: BLE001
                    _log.warning("Polling failed for device %r: %s",
                                 device.device_id, exc)

            # Sessions + vehicle SoC: lays each vehicle's SoC over its charger.
            now = datetime.now(UTC)
            changes = self._fleet.compose(states, now)

            for state in states.values():
                try:
                    self._registry.update_state(state)
                    await self._bus.publish(DeviceStateEvent(state=state))
                    await self._storage.write(
                        Measurement(
                            device_id=state.device_id,
                            timestamp=state.timestamp,
                            power_w=state.power_w,
                            energy_kwh=state.energy_kwh,
                            soc_pct=state.soc_pct,
                            # extra is never read back from history — storing
                            # its JSON on every poll only bloated the DB.
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("Recording failed for device %r: %s", state.device_id, exc)

            await self._bus.flush()

            if changes:
                # On the first poll the planning loop's first run picks them up.
                await self._on_session_changes(changes, replan=not first_tick, now=now)
            if first_tick:
                await self._init_ledger(states)
                first_tick = False
                self._first_poll_done.set()  # unblock planning and control loops
            await self._sync_vehicles(states)

            await asyncio.sleep(self._poll_interval_s)

    # ------------------------------------------------------------------
    # Vehicles and charging sessions
    # ------------------------------------------------------------------

    def _apply_sessions(self) -> None:
        """Recompose the vehicle assets and contributors after a session change."""
        self._ev_assets[:] = self._fleet.assets()
        self._fleet.apply(self._disabled_chargepoints, self._ev_force_charge)

    async def _on_session_changes(
        self, changes: list[SessionChange], *, replan: bool, now: datetime | None = None,
    ) -> None:
        """Persist session changes and react: plug-in time, force charge, openWB
        vehicle assignment, an immediate replan.

        The plug-in time gates the missed-deadline catch-up (see
        ``resolve_active_goals``): set when a vehicle arrives at a charger —
        also on the first poll after a restart, so how long it had been
        plugged in before is unknown and counts as a fresh plug-in — and
        cleared when it leaves.
        """
        now = now or datetime.now(UTC)
        for ch in changes:
            vehicle = ch.vehicle_id or "guest"
            if ch.started:
                session = self._fleet.session(ch.charger_id)
                if session is not None:
                    session.session_id = await self._storage.open_vehicle_session(
                        ch.charger_id, ch.vehicle_id, session.started_at, now, session.source,
                    )
                _log.info("Session: %s at %s (%s)", vehicle, ch.charger_id,
                          session.source if session else "?")
                if ch.vehicle_id is not None:
                    self._ev_plugged_since[ch.vehicle_id] = now
                await self._sync_charger_assignment(ch.charger_id)
            else:
                await self._storage.close_vehicle_sessions(ch.charger_id, now)
                _log.info("Session: %s left %s%s", vehicle, ch.charger_id,
                          " (unplugged)" if ch.unplugged else "")
                if ch.vehicle_id is not None:
                    self._ev_plugged_since[ch.vehicle_id] = None
                    if ch.vehicle_id in self._ev_force_charge:
                        await self._clear_force_charge(
                            ch.vehicle_id,
                            "vehicle unplugged" if ch.unplugged else "vehicle reassigned",
                        )
        if any(ch.started for ch in changes):  # a pre-selection may have been used up
            await self._save_selections()
        self._apply_sessions()
        if replan:
            # A car arriving shortly after a planning cycle must not wait up
            # to plan_interval_s for a goal.
            asyncio.create_task(self._run_plan())

    async def _sync_charger_assignment(self, charger_id: str) -> None:
        """Make the charger system's own vehicle assignment match the session
        (openWB: the vehicle whose SoC limit and profile it uses)."""
        device = self._registry.get(charger_id)
        session = self._fleet.session(charger_id)
        if session is None or device is None or not hasattr(device, "assign_vehicle"):
            return
        wanted = self._fleet.charger_vehicle_id(session.vehicle_id, charger_id)
        # A manual reassignment is always passed on (the cached assignment may
        # be stale right after the previous one); on plug-in only when the
        # charger system has another vehicle assigned.
        current = getattr(device, "assigned_vehicle_id", None)
        if wanted is None or self._dry_run or (session.source != MANUAL and wanted == current):
            return
        try:
            await device.assign_vehicle(wanted)
            _log.info("Charger %s: assigned vehicle id %s in the charger system", charger_id, wanted)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Charger %s: could not assign vehicle %s: %s", charger_id, wanted, exc)

    async def _sync_vehicles(self, states: dict[str, DeviceState]) -> None:
        """Push vehicle SoCs to their charger systems; press a car's refresh
        button when its SoC is old while it charges."""
        mono = time.monotonic()
        for vid, est in self._fleet.estimators.items():
            soc = est.state.soc_pct
            spec = self._fleet.vehicles[vid]
            target = self._fleet.soc_push_target(vid)
            if soc is not None and target is not None and not self._dry_run:
                cid, ext_id = target
                last = self._soc_pushed.get(vid)
                key = round(soc, 1)
                if last is None or (last[0], last[1]) != (key, est.state.reported_at) and \
                        mono - last[2] >= _SOC_PUSH_MIN_INTERVAL_S:
                    device = self._registry.get(cid)
                    vs = states.get(vid)
                    try:
                        await device.push_vehicle_soc(  # type: ignore[union-attr]
                            ext_id, soc, vs.extra.get("range_km") if vs else None,
                            est.state.reported_at,
                        )
                        self._soc_pushed[vid] = (key, est.state.reported_at, mono)
                    except Exception as exc:  # noqa: BLE001
                        _log.debug("Vehicle %s: SoC push to %s failed: %s", vid, cid, exc)

            refresh_min = spec.params.get("refresh_after_min")
            cid = self._fleet.sessions.charger_of(vid)
            device = self._registry.get(vid)
            charger = states.get(cid) if cid else None
            reported = est.state.reported_at
            if (
                refresh_min and charger is not None and (charger.power_w or 0) > 100
                and device is not None and getattr(device, "can_refresh", False)
                and reported is not None
                and datetime.now(UTC) - reported > timedelta(minutes=refresh_min)
                and mono - self._refresh_pressed.get(vid, -1e9) > refresh_min * 60
                and not self._dry_run
            ):
                self._refresh_pressed[vid] = mono
                try:
                    await device.refresh()  # type: ignore[attr-defined]
                    _log.info("Vehicle %s: SoC older than %g min while charging — refresh "
                              "requested", vid, refresh_min)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("Vehicle %s: refresh failed: %s", vid, exc)

    async def select_charger(self, vehicle_id: str | None, charger_id: str | None) -> None:
        """Put a car (``None`` = a guest) on *charger_id* — the last selection
        wins (see ``SessionManager.select``); ``charger_id=None`` drops the
        car's selections.  Raises ValueError."""
        changes = self._fleet.sessions.select(vehicle_id, charger_id, datetime.now(UTC))
        await self._save_selections()
        _log.info("Charger selected: %s → %s", vehicle_id or "guest", charger_id or "none")
        if changes:
            await self._on_session_changes(changes, replan=True)
        else:
            self._apply_sessions()

    async def set_guest_mode(self, charger_id: str | None, mode: str) -> None:
        """Charge the guest car at *charger_id* (None: wherever the guest is)
        ``pv_only`` or ``instant``.  Raises ValueError."""
        if charger_id is None:
            charger_id = next((c for c in self._fleet.chargers if self._fleet.sessions.guest_at(c)), None)
            if charger_id is None:
                raise ValueError("no guest car at any charger — put the guest on a charger first")
        self._fleet.sessions.set_guest_mode(charger_id, mode)
        await self._save_selections()
        self._apply_sessions()
        _log.info("Guest car at %s: %s", charger_id, mode)
        asyncio.create_task(self._run_plan())

    async def _save_selections(self) -> None:
        """Store the cars expected at the chargers' next plug-ins and the guest modes."""
        self._fleet.sessions.prune_guest_modes()
        await self._storage.save_next_vehicles(dict(self._fleet.sessions.next_vehicle))
        await self._storage.save_guest_modes(dict(self._fleet.sessions.guest_modes))

    def _charger_status(self, status: dict) -> dict:
        """``SessionManager.status`` with display names for the API."""
        out = dict(status)
        cid = status.get("charger_id")
        out["label"] = self._fleet.chargers[cid].display_name if cid in self._fleet.chargers else None
        by = status.get("by")
        if by is not None:
            out["by_label"] = (
                "Guest" if by == "guest"
                else self._fleet.vehicles[by].display_name if by in self._fleet.vehicles else by
            )
        return out

    # ------------------------------------------------------------------
    # Planning loop
    # ------------------------------------------------------------------

    async def _planning_loop(self) -> None:
        """Run the MILP optimizer periodically and publish the resulting plan.

        After an immediate first run, runs are aligned to the slot grid
        (``plan_lead_s`` before a boundary) instead of sleeping a fixed
        interval, so the optimizer's runtime never accumulates into drift.
        Event-triggered replans run outside this schedule and don't move it.
        """
        await self._first_poll_done.wait()  # ensure registry has live SoC before first run
        period = aligned_plan_period(self._plan_interval_s, self._optimizer._step_min)
        if period.total_seconds() != self._plan_interval_s:
            _log.warning(
                "plan_interval_s=%g is not aligned with %d-min slots — planning every %d s instead",
                self._plan_interval_s, self._optimizer._step_min, period.total_seconds(),
            )
        # The lead must leave the run inside the period it belongs to.
        lead = timedelta(seconds=min(max(0.0, self._plan_lead_s), period.total_seconds() / 2))
        scheduled: datetime | None = None
        while True:
            try:
                await self._run_plan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("Planning run failed, will retry: %s", exc)
            now = datetime.now(timezone.utc)
            scheduled = next_plan_run(now, period, lead, after=scheduled)
            _log.debug("Next planning run at %s", scheduled.isoformat())
            await asyncio.sleep((scheduled - now).total_seconds())

    async def _battery_refill(
        self, effective_horizon: timedelta, batteries: list[StorageConstraints]
    ) -> dict[str, float]:
        """PV surplus each battery can take in during the 24 h after the
        plan (see ``_battery_refill_kwh``); empty when unknown."""
        start = datetime.now(UTC) + effective_horizon
        span = effective_horizon + _REFILL_WINDOW + timedelta(hours=1)
        providers = [
            p for p in self._forecast_providers
            if getattr(p, "quantity", None) in (ForecastQuantity.PV_GENERATION, ForecastQuantity.CONSUMPTION)
        ]
        try:
            ahead = await _collect_forecasts(providers, span)
            refill = _battery_refill_kwh(
                ahead.get(ForecastQuantity.PV_GENERATION, []),
                ahead.get(ForecastQuantity.CONSUMPTION, []),
                start, batteries,
            )
        except Exception as exc:  # noqa: BLE001 — never break planning
            _log.warning("PV refill after the horizon unavailable: %s", exc)
            return {}
        self._last_battery_refill = refill
        if refill:
            _log.debug("PV refill in the 24 h after the plan: %s kWh", refill)
        return refill

    async def _run_plan(self) -> None:
        """Assemble context, optimize, publish plan, refresh price cache."""
        if self._reloading:
            return  # the new runtime plans as soon as it is up
        self._plan_active_count += 1
        try:
            await self._run_plan_inner()
        finally:
            self._plan_active_count -= 1

    async def _run_plan_inner(self) -> None:
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
                            _log.debug(
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
                        _log.debug(
                            "_run_plan: soc_pct missing for %r — using ledger %.2f kWh → %.1f%%",
                            sc.device_id, stored_kwh, derived_soc,
                        )

        forecasts = await _collect_forecasts(self._forecast_providers, self._horizon)

        # Price the plan per metering zone: the default tariff's price is what
        # every flexible device pays, and consumers on another tariff (e.g. a
        # heat pump) are handed to the optimizer as separately billed loads.
        (
            primary_prices,
            price_estimated_by_ts,
            secondary_loads,
        ) = await self._build_tariff_zone_inputs()
        if primary_prices:
            forecasts[ForecastQuantity.PRICE] = primary_prices
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

        # Compute active EV goals from current SoC + weekly plan/overrides.
        # Disabled chargepoints are excluded from planning and control;
        # force-charging ones are excluded from planning (control bypasses
        # the plan while forced, and the next re-solve sees the raised SoC).
        await self._purge_expired_ev_overrides()
        excluded_disabled = [a for a in self._ev_assets if a.asset_id in self._disabled_chargepoints]
        excluded_force = [a for a in self._ev_assets if a.asset_id in self._ev_force_charge]
        active_assets = [
            a for a in self._ev_assets
            if a.asset_id not in self._disabled_chargepoints
            and a.asset_id not in self._ev_force_charge
        ]
        if excluded_disabled or excluded_force:
            _log.debug(
                "EV planning exclusions: disabled=%s force_charge=%s",
                [a.asset_id for a in excluded_disabled],
                [a.asset_id for a in excluded_force],
            )
        ev_goals = resolve_active_goals(
            active_assets, device_states, self._ev_weekly_plans, self._ev_day_overrides,
            overdue_dismissed=self._ev_overdue_dismissed,
            plugged_since=self._ev_plugged_since,
            feasibility_margin=self._ev_feasibility_margin,
        )
        self._last_ev_goals = ev_goals
        self._update_savings_ev_needs(ev_goals, device_states)
        # Assets with a chargepoint that isn't currently reporting `connected`
        # get no goal at all this cycle — worth surfacing since it means the
        # car won't be charged even if a schedule exists for it.
        goal_device_ids = {g.device_id for g in ev_goals}
        for asset in active_assets:
            if not asset.device_id:
                continue  # vehicle away: not at any charger
            state = device_states.get(asset.device_id)
            connected = state is not None and state.available
            if not connected and asset.device_id not in goal_device_ids:
                _log.debug(
                    "EV %r: chargepoint %r not connected/available — no goal this cycle",
                    asset.asset_id, asset.device_id,
                )
        # Push updated goals to contributors so the control loop uses them.
        # Also propagate the target SoC to devices that write it to hardware
        # (e.g. openWB instant-charging SoC limit register).  While a force
        # charge is active its target wins.
        assets_by_device = {a.device_id: a for a in self._ev_assets}
        for contrib in self._ev_contributors:
            # Only the goal of the vehicle still at this charger (a session
            # may have changed while the plan was being solved).
            goal = next(
                (g for g in ev_goals
                 if g.device_id == contrib.device_id and g.asset_id == contrib.asset.asset_id),
                None,
            )
            contrib.update_goal(goal)
            device = self._registry.get(contrib.device_id)
            if device is not None and hasattr(device, "update_target_soc"):
                asset = assets_by_device.get(contrib.device_id)
                force = self._ev_force_charge.get(asset.asset_id) if asset else None
                device.update_target_soc(
                    force if force is not None else (goal.target_soc_pct if goal else None)
                )
        if ev_goals:
            _log.info(
                "EV goals: %s",
                [
                    {
                        "asset": g.asset_id,
                        "soc": f"{g.current_soc_pct:.0f}%→{g.target_soc_pct:.0f}%",
                        "target_by": g.target_by.strftime("%Y-%m-%dT%H:%M"),
                        "phase1_kwh": round(g.phase1_required_kwh, 1),
                        "phase2_kwh": round(g.phase2_required_kwh, 1),
                        "pv_only": g.pv_only,
                        "infeasible": g.infeasible,
                        "overdue": g.overdue,
                    }
                    for g in ev_goals
                ],
            )

        thermal_models, ambient_forecasts = await self._thermal_inputs(effective_horizon)
        storage_constraints = self._planning_storage_constraints(refit=True)
        self._savings.set_batteries(storage_constraints)
        battery_refill = await self._battery_refill(effective_horizon, storage_constraints)
        context = OptimizationContext(
            device_states=device_states,
            storage_constraints=storage_constraints,
            threshold_constraints=self._threshold_constraints,
            thermal_models=thermal_models,
            ambient_forecasts=ambient_forecasts,
            tariffs=self._tariffs,
            forecasts=forecasts,
            horizon=effective_horizon,
            battery_cost_basis=self._ledger.all_cost_bases(),
            battery_refill_kwh=battery_refill,
            ev_charging_goals=ev_goals,
            secondary_loads=secondary_loads,
            max_import_kw=self._topology.max_import_kw if self._topology else None,
            producer_device_ids={
                d.device_id for d in self._registry.by_category(DeviceCategory.PV)
            },
        )

        # Two-stage solve: when no plan is active yet (first run after
        # startup), compute a coarse fast-pass plan immediately so the UI
        # has data, then refine with the configured-precision solve below.
        # The fast plan is DISPLAY-ONLY (never published to the control loop):
        # its coarse decisions can contradict the refined plan seconds later,
        # and actuating hardware on it caused e.g. a spurious cooler start
        # that the compressor min-runtime then locked in for 30 minutes.
        active = self._control_loop._active_plan
        if active is None or not active.intents:
            try:
                fast_plan = await self._fast_optimizer.optimize(context)
                if fast_plan.intents:
                    _log.info(
                        "Fast first-pass plan: %d intents (display-only) — "
                        "refining at full precision…",
                        len(fast_plan.intents),
                    )
                    self._display_plan = fast_plan
                    self._plan_seq += 1  # notify SSE clients
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

        try:
            await self._storage.save_plan_snapshot(plan, day_ahead=self._is_day_ahead_plan(plan))
        except Exception as exc:  # noqa: BLE001 — reporting must never block planning
            _log.warning("Could not save plan snapshot: %s", exc)

        if self._mqtt_publisher is not None:
            # override_active per asset = the staged deadline's local date has
            # a dated override (see _mqtt_enable_override).
            mqtt_overrides = {
                a.asset_id: staged
                for a in self._ev_assets
                if (staged := self._staged_overrides.get(a.asset_id)) is not None
                and self._mqtt_override_active(a.asset_id)
            }
            await self._mqtt_publisher.publish_states(
                ev_goals, device_states, mqtt_overrides, self._staged_overrides
            )
            try:
                await self._publish_price_curve()
            except Exception as exc:  # noqa: BLE001 — a dashboard must never block planning
                _log.warning("Could not publish price curve: %s", exc)

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    async def _control_task(self) -> None:
        """Send setpoints on every control tick based on the active plan."""
        await self._first_poll_done.wait()  # ensure registry and plan are ready
        # Whatever a previous run left behind (a crash during a forced charge)
        # ends here; the first control tick commands the devices again.
        await self._release_devices()

        last = time.monotonic()
        while True:
            now_mono = time.monotonic()
            dt_hours = (now_mono - last) / 3600.0
            last = now_mono

            try:
                await self._do_control_tick(dt_hours)
                self._failsafe.tick_done()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A transient failure (e.g. ioBroker restarting during a
                # container update) must not kill the task: asyncio.gather in
                # run_forever would propagate it and terminate the process.
                _log.warning("Control tick failed, will retry: %s", exc)
            try:
                await self._publish_ev_live_states()
                if self._mqtt_publisher is not None:
                    await self._mqtt_publisher.publish_readings({
                        device.device_id: state
                        for device in self._registry.all()
                        if (state := self._registry.latest_state(device.device_id)) is not None
                    })
                    await self._mqtt_publisher.publish_current_price()
            except Exception as exc:  # noqa: BLE001 — reporting must never block control
                _log.warning("Could not publish EV live states: %s", exc)
            await asyncio.sleep(self._control_interval_s)

    async def _publish_ev_live_states(self) -> None:
        """Push each EV's charging state (UI mode) and commanded openWB mode
        to MQTT.  Runs every control tick; the publisher dedups."""
        if self._mqtt_publisher is None:
            return
        now = datetime.now(timezone.utc)
        plan = self._display_plan or self._control_loop._active_plan
        setpoints = self._control_loop.last_setpoints
        behaviours = self._control_loop.last_behaviours
        states: dict[str, tuple[str | None, str | None]] = {}
        for asset in self._ev_assets:
            goal = next((g for g in self._last_ev_goals if g.asset_id == asset.asset_id), None)
            charging_state = ev_current_mode(
                plan, asset.device_id, goal.phase2_start_time if goal else None, now,
                force_charge=asset.asset_id in self._ev_force_charge,
                disabled=asset.asset_id in self._disabled_chargepoints,
            )
            # No tick has computed setpoints yet (no plan) → leave mode as is.
            charging_mode = (
                charging_mode_from_behaviour(
                    behaviours.get(asset.device_id),
                    disabled=asset.asset_id in self._disabled_chargepoints,
                )
                if asset.device_id in setpoints
                else None
            )
            states[asset.asset_id] = (charging_state, charging_mode)
        await self._mqtt_publisher.publish_live_states(states)

    async def _check_force_charge_reset(
        self, device_states: dict[str, Any], now: datetime | None = None,
    ) -> None:
        """Clear a force charge once its target SoC is reached.

        Plug-in and unplug are handled by the charging sessions
        (``_on_session_changes``): they set when each car was plugged in and
        clear a force charge when the car leaves.  The car's SoC at the
        charger is the vehicle's (estimated) SoC laid over the charger state.
        """
        for asset in self._ev_assets:
            target = self._ev_force_charge.get(asset.asset_id)
            if target is None or not asset.device_id:
                continue
            state = device_states.get(asset.device_id)
            if state is not None and state.soc_pct is not None and state.soc_pct >= target:
                await self._clear_force_charge(asset.asset_id, f"target {target:.0f}% reached")

    def _grid_limit(self) -> GridLimit | None:
        """Grid connection limit from ``max_import_kw`` on the topology root."""
        root = self._topology
        if root is None or root.max_import_kw is None:
            return None
        limit = GridLimit(
            max_import_w=root.max_import_kw * 1000.0,
            curtail_order=tuple(root.curtail_order),
            hysteresis_w=(
                root.limit_hysteresis_kw * 1000.0
                if root.limit_hysteresis_kw is not None else None
            ),
        )
        _log.info(
            "Grid connection limit %.1f kW (release below %.1f kW), curtail order: %s",
            root.max_import_kw, limit.release_w / 1000.0,
            ", ".join(root.curtail_order) or "storage, then EVs",
        )
        return limit

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

        await self._check_force_charge_reset(device_states)

        tariff_prices = await self._fetch_tariff_prices(now)

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
            secondary_price_eur_per_kwh=self._secondary_import_price(tariff_prices, device_states),
        )

        self._sync_ledger_stored_energy_from_soc()

        try:
            await self._track_savings(now, current_price, device_states)
        except Exception as exc:  # noqa: BLE001 — reporting must never block control
            # Runs every few seconds: warn once per distinct failure.
            if self._dry_run_log_gate.changed("savings_error", repr(exc)):
                _log.warning("Savings tracking failed: %s", exc)

        if self._dry_run:
            # Only log rows whose (mode, setpoint) actually changed since the
            # last tick — otherwise this fires every control_interval_s even
            # when nothing changed.
            changed_rows: list[tuple[str, int]] = []
            for device_id, setpoint_w, mode in self._control_loop.describe_setpoints(live):
                # Some hardware has no register to command charge power —
                # only discharge is ever actively written, and charge/idle
                # both collapse to the same "clear discharge limit" no-op.
                # Reflect that instead of implying a specific charge wattage
                # actually reaches the device.
                device = self._registry.get(device_id)
                controls_charge = getattr(device, "controls_charge", True)

                if setpoint_w is None:
                    key: tuple[str, int | None] = (mode, None)
                    row = f"DRY RUN  {device_id}  mode={mode:<10}  → skip (no setpoint)"
                    level = logging.INFO
                elif setpoint_w > 0 and not controls_charge:
                    key = (mode, 0)
                    row = f"DRY RUN  {device_id}  mode={mode:<10}  → no active command (charges automatically)"
                    level = logging.DEBUG
                elif setpoint_w > 0:
                    key = (mode, int(round(setpoint_w)))
                    row = f"DRY RUN  {device_id}  mode={mode:<10}  → charge   {setpoint_w:+.0f} W"
                    level = logging.INFO
                elif setpoint_w < 0:
                    key = (mode, int(round(setpoint_w)))
                    row = f"DRY RUN  {device_id}  mode={mode:<10}  → discharge {setpoint_w:+.0f} W"
                    level = logging.INFO
                else:
                    key = (mode, 0)
                    row = f"DRY RUN  {device_id}  mode={mode:<10}  → hold (0 W)"
                    level = logging.INFO if controls_charge else logging.DEBUG
                if self._dry_run_log_gate.changed(device_id, key):
                    changed_rows.append((row, level))
            if changed_rows:
                _log.debug(
                    "DRY RUN tick  grid=%.0f W  price=%.4f €/kWh  dt=%.4f h",
                    grid_power_w, current_price, dt_hours,
                )
                for row, level in changed_rows:
                    _log.log(level, row)
            await self._record_execution(now, device_states)
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

        command = await self._failsafe.check_plan(self._control_loop._active_plan, now)
        await self._control_loop.tick(live, self._registry, send_commands=command)
        await self._record_execution(now, device_states)

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

    async def _record_execution(self, now: datetime, device_states: dict[str, Any]) -> None:
        """Book this tick's intents, setpoints and measured power into the
        execution log (core/execution_log.py); persist finished slots."""
        if self._execution_log is None:
            return
        try:
            loop = self._control_loop
            ticks = {}
            for device_id, mode in loop.last_modes.items():
                intent = loop.last_intents.get(device_id)
                state = device_states.get(device_id)
                ticks[device_id] = DeviceTick(
                    mode=mode,
                    planned_w=intent.power_kw * 1000.0 if intent is not None else None,
                    setpoint_w=loop.last_setpoints.get(device_id),
                    measured_w=state.power_w if state is not None else None,
                )
            plan = loop._active_plan
            finished = self._execution_log.record(
                now, ticks, plan.created_at if plan is not None else None
            )
            if finished:
                await self._storage.save_execution_slots(finished)
        except Exception as exc:  # noqa: BLE001 — reporting must never block control
            if self._dry_run_log_gate.changed("execution_error", repr(exc)):
                _log.warning("Execution log failed: %s", exc)

    # ------------------------------------------------------------------
    # Savings accounting
    # ------------------------------------------------------------------

    async def _load_savings_state(self) -> SavingsState | None:
        """Restore the savings state, unless it is stale or of an older format."""
        saved = await self._storage.load_savings_state()
        if saved is None:
            return None
        saved_at, data = saved
        if datetime.now(timezone.utc) - saved_at > _SAVINGS_STATE_MAX_AGE:
            _log.info("Savings: state from %s is stale — starting fresh", saved_at)
            return None
        state = SavingsState.from_dict(data)
        if state is None:
            _log.info("Savings: saved state has an older format — starting fresh")
        return state

    def _secondary_consumers(self) -> dict[str, list[str]]:
        """Measured consumers billed on a tariff other than the default one,
        keyed by tariff id (e.g. the heat pump on its own tariff)."""
        primary_id = self._cfg.default_tariff_id
        out: dict[str, list[str]] = defaultdict(list)
        for device_id, spec in self._cfg.devices.items():
            device = self._registry.get(device_id)
            if device is None or device.category is not DeviceCategory.CONSUMER:
                continue
            tariff_id = spec.tariff or primary_id
            if tariff_id != primary_id and tariff_id in self._tariffs:
                out[tariff_id].append(device_id)
        return out

    def _secondary_import_price(
        self, tariff_prices: dict[str, float], device_states: dict[str, Any]
    ) -> float | None:
        """Current import price (€/kWh) of the separately billed loads.

        Weighted by their measured power; the plain mean of their tariffs
        when none is drawing.  ``None`` when the site has no such loads.
        """
        weighted = 0.0
        total_w = 0.0
        prices: list[float] = []
        for tariff_id, device_ids in self._secondary_consumers().items():
            price = tariff_prices.get(tariff_id)
            if price is None:
                continue
            prices.append(price)
            for did in device_ids:
                st = device_states.get(did)
                w = max(0.0, st.power_w) if st is not None and st.power_w is not None else 0.0
                weighted += w * price
                total_w += w
        if not prices:
            return None
        return weighted / total_w if total_w > 0.0 else sum(prices) / len(prices)

    async def _savings_sample(
        self, now: datetime, import_price: float, device_states: dict[str, Any]
    ) -> LiveSample | None:
        """Snapshot of the real site, or ``None`` if a reading is missing."""
        max_age = timedelta(seconds=max(60.0, 10 * self._poll_interval_s))

        def power_kw(device_id: str) -> float | None:
            st = device_states.get(device_id)
            if st is None or st.power_w is None or now - st.timestamp > max_age:
                return None
            return st.power_w / 1000.0

        if not self._grid_meter_id or import_price <= 0.0:
            return None
        site_kw = power_kw(self._grid_meter_id)
        if site_kw is None or not device_states[self._grid_meter_id].available:
            return None

        storage_kw: dict[str, float] = {}
        stored_kwh: dict[str, float] = {}
        for sc in self._storage_constraints:
            kw = power_kw(sc.device_id)
            stored = self._ledger.stored_energy(sc.device_id)
            if kw is None or stored is None:
                return None
            storage_kw[sc.device_id] = kw
            stored_kwh[sc.device_id] = stored

        # Per charger: whichever car (or guest) is plugged in there.
        ev: dict[str, EvLive] = {}
        for cid, contrib in self._fleet.contributors.items():
            st = device_states.get(cid)
            plugged = st.extra.get("plugged") if st is not None else None
            ev[cid] = EvLive(
                plugged=plugged if isinstance(plugged, bool) else None,
                power_kw=max(0.0, (st.power_w or 0.0) / 1000.0) if st is not None else 0.0,
                max_kw=contrib.asset.max_charge_kw,
            )

        tariff_prices = await self._fetch_tariff_prices(now)
        secondary: list[tuple[float, float]] = []
        for tariff_id, device_ids in self._secondary_consumers().items():
            price = tariff_prices.get(tariff_id)
            if price is None:
                continue  # no price → billed with the rest at the default price
            kws = [power_kw(did) for did in device_ids]
            if any(kw is None for kw in kws):
                return None
            secondary.append((sum(max(0.0, kw) for kw in kws), price))

        return LiveSample(
            timestamp=now,
            site_net_kw=site_kw,
            storage_kw=storage_kw,
            stored_kwh=stored_kwh,
            ev=ev,
            secondary=secondary,
            import_price=import_price,
            export_price=self._pv_opportunity_price,
            cost_basis=self._ledger.all_cost_bases(),
            pv_kw=abs(pv) if self._pv_device_id and (pv := power_kw(self._pv_device_id)) is not None
            else None,
            devices={
                tc.device_id: DeviceLive(
                    power_kw=power_kw(tc.device_id),
                    value=_float_or_none(
                        (device_states.get(tc.device_id).extra or {}).get("measured_value")
                        if device_states.get(tc.device_id) is not None else None
                    ),
                )
                for tc in self._threshold_constraints
            },
        )

    async def _track_savings(
        self, now: datetime, import_price: float, device_states: dict[str, Any]
    ) -> None:
        """Book this control tick into the savings accounting."""
        sample = await self._savings_sample(now, import_price, device_states)
        if sample is None:
            return
        finished = self._savings.tick(sample)
        for slot in finished:
            await self._save_savings_slot(slot)
        corrections = self._savings.pop_corrections()
        for corr in corrections:
            if not await self._storage.apply_savings_correction(corr):
                _log.info("Savings: no stored slot %s for a car correction", corr.start.isoformat())
        if corrections:
            _log.info(
                "Savings: car unplugged before its deadline — uncontrolled car recalculated"
                " back from unplug: %.2f kWh in %d earlier slot(s), %+.3f €",
                sum(c.uncontrolled_ev_kwh for c in corrections), len(corrections),
                sum(c.ev_saving for c in corrections),
            )
            await self._publish_savings()

    async def _save_savings_slot(self, slot: SavingsSlot) -> None:
        await self._storage.save_savings_slot(slot)
        await self._storage.save_savings_state(self._savings.state.to_dict())
        _log.info(
            "Savings %s: %+.3f € (uncontrolled %.3f €, real %.3f €, of which cars %+.3f €)",
            slot.start.astimezone().strftime("%H:%M"), slot.saving,
            slot.uncontrolled_cost, slot.real_cost, slot.ev_saving,
        )
        await self._publish_savings()

    async def _publish_price_curve(self) -> None:
        """Push import, feed-in and effective price per slot to Home Assistant.

        Uses the same aligned series as ``/api/forecast``; already-passed
        slots of the day are kept by ``PriceCurveHistory`` so a dashboard can
        show the whole day (see ``core/price_curve.py``).
        """
        if self._mqtt_publisher is None:
            return
        fc = await self._aligned_forecast()
        timestamps = [datetime.fromisoformat(t) for t in fc["timestamps"]]
        if not timestamps:
            return
        prices = fc.get("variable_prices") or fc["prices"]
        estimated = fc.get("variable_price_is_estimated") or fc.get("price_is_estimated") or []
        slots = build_price_slots(
            timestamps,
            prices,
            fc["export_prices"],
            fc["pv_kw"],
            fc["consumption_kw"],
            [bool(e) for e in estimated] or [False] * len(timestamps),
        )
        past = [
            (p.timestamp, float(p.value))
            for p in (self._last_variable_price_pts or self._last_forecast_pts.get(ForecastQuantity.PRICE, []))
        ]
        curve = self._price_history.update(slots, datetime.now(timezone.utc), past)
        await self._mqtt_publisher.publish_price_curve(curve, int(fc["step_minutes"]))

    async def _publish_savings(self) -> None:
        """Push the all-time savings totals to Home Assistant."""
        if self._mqtt_publisher is None:
            return
        rows = await self._storage.query_savings_slots(
            datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc)
        )
        await self._mqtt_publisher.publish_savings(_savings_totals(rows))

    def _update_savings_ev_needs(
        self, ev_goals: list[EvChargingGoal], device_states: dict[str, Any]
    ) -> None:
        """Give each plugged-in car's charge plan to the uncontrolled car.

        With a deadline it charges as late as possible to reach the target; a
        PV-only plan charges from PV only; no plan (disabled chargepoint,
        force charge, no schedule) charges like the real car.
        """
        goals = {g.device_id: g for g in ev_goals}
        for cid in self._fleet.chargers:
            st = device_states.get(cid)
            if st is None or st.extra.get("plugged") is not True:
                continue
            goal = goals.get(cid)
            if goal is None or not goal.connected:
                self._savings.set_ev_goal(cid, None, None)
            elif goal.pv_only:
                self._savings.set_ev_goal(cid, None, None, pv_only=True)
            else:
                self._savings.set_ev_goal(
                    cid,
                    goal.phase1_required_kwh + goal.phase2_required_kwh,
                    goal.target_by,
                )

    def _savings_groups(self) -> list[dict[str, Any]]:
        """Controlled device groups the Savings tab shows one panel each for:
        the batteries together, each car, each threshold device.  ``id`` is
        the key in a slot's ``components`` / ``detail``."""
        groups: list[dict[str, Any]] = []
        if self._storage_constraints:
            groups.append({"id": "batteries", "kind": "batteries", "label": "Batteries"})
        # Savings are kept per charger (whichever car charged there), named
        # after the charger's default vehicle.
        for cid, spec in self._fleet.chargers.items():
            default = spec.params.get("default_vehicle")
            label = (self._fleet.vehicles[default].display_name if default else spec.display_name)
            groups.append({"id": cid, "kind": "car", "label": label})
        for tc in self._threshold_constraints:
            groups.append({
                "id": tc.device_id, "kind": "device", "label": tc.label or tc.device_id,
                "unit": tc.unit, "bottom_threshold": tc.bottom_threshold,
                "top_threshold": tc.top_threshold,
            })
        return groups

    def _planning_storage_constraints(self, refit: bool = False) -> list[StorageConstraints]:
        """Storage constraints the plan runs on: the configured ones, with the
        learned efficiency for batteries set to ``efficiency: learned`` once
        their estimate is confident.  The savings accounting runs on the
        same values (see plugins/learned/efficiency)."""
        out = []
        for sc in self._storage_constraints:
            learner = self._efficiency_learners.get(sc.device_id)
            if learner is None:
                out.append(sc)
                continue
            if refit:
                learner.schedule_refit()
            out.append(learner.apply(sc))
        return out

    async def _thermal_inputs(self, horizon: timedelta) -> tuple[dict, dict]:
        """Current thermal model and ambient forecast per threshold device.

        Refits run in the background when due, so planning never waits for
        Home Assistant's history; the next plan picks up the new model.
        """
        models: dict[str, Any] = {}
        ambients: dict[str, Any] = {}
        for device_id, learner in self._thermal_learners.items():
            learner.schedule_refit()
            models[device_id] = learner.model()
            try:
                ambients[device_id] = await learner.ambient_forecast(horizon)
            except Exception as exc:  # noqa: BLE001 — planning must not fail on it
                _log.debug("thermal:%s: ambient forecast failed: %s", device_id, exc)
            # The savings' uncontrolled thermostat runs on the same model.
            points = ambients.get(device_id) or []
            self._savings.set_thermal_model(
                device_id, models[device_id], points[0].value if points else None
            )
        return models, ambients

    def _is_day_ahead_plan(self, plan: EnergyPlan) -> bool:
        """True for the first plan made shortly before local midnight — the
        plan the coming day starts with (flagged in the plan snapshots)."""
        now = datetime.now(timezone.utc)
        lead = aligned_plan_period(self._plan_interval_s, plan.step_minutes) + timedelta(
            minutes=plan.step_minutes
        )
        coming = (day_ahead_window_end(now, lead) - timedelta(hours=12)).astimezone().date()
        if coming <= now.astimezone().date() or coming == self._day_ahead_flagged:
            return False
        self._day_ahead_flagged = coming
        return True

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    def _build_api(self) -> FastAPI:
        """Build the FastAPI application exposing live server state."""
        mcp_holder: list = []

        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI):
            # The MCP endpoint's session manager must run for the app's lifetime.
            async with contextlib.AsyncExitStack() as stack:
                for mcp in mcp_holder:
                    await stack.enter_async_context(mcp.session_manager.run())
                yield

        api = FastAPI(title="Energy Assistant", version=version_label(), lifespan=lifespan)

        @api.get("/health")
        async def health() -> dict:
            """Liveness probe endpoint used by container health checks."""
            return {"status": "ok", "configuration": self.configuration_state()["state"]}

        @api.get("/api/version")
        async def get_version() -> dict:
            """Version of the running build (shown in the UI, asked for in problem reports)."""
            return version_info()

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
                secondary_price_eur_per_kwh=self._secondary_import_price(
                    tariff_prices_status, device_states_map,
                ),
            )

            setpoint_rows = self._control_loop._compute_setpoints(live)
            behaviours = self._control_loop.behaviours(setpoint_rows, live)

            devices_payload = []
            for s in device_states_map.values():
                spec = self._cfg.devices.get(s.device_id)
                devices_payload.append(
                    {
                        "device_id": s.device_id,
                        "label": (
                            spec.display_name if spec is not None
                            else s.device_id.replace("_", " ").title()
                        ),
                        "category": spec.category.value if spec is not None else None,
                        "power_w": s.power_w,
                        "soc_pct": s.soc_pct,
                        "available": s.available,
                        "timestamp": s.timestamp.isoformat(),
                        "is_virtual": spec is not None and spec.type == "generic_consumer",
                        "forecast_power_w": await _virtual_forecast_power_w(
                            spec, getattr(self, "_device_forecast_providers", {}).get(s.device_id)
                        ),
                    }
                )
            # Devices that have not answered once since the start (unreachable):
            # shown as unavailable rather than left out.
            if self._first_poll_done.is_set():
                for device in self._registry.all():
                    if device.device_id in device_states_map:
                        continue
                    spec = self._cfg.devices.get(device.device_id)
                    devices_payload.append({
                        "device_id": device.device_id,
                        "label": self._label(device.device_id),
                        "category": device.category.value,
                        "power_w": None,
                        "soc_pct": None,
                        "available": False,
                        "timestamp": None,
                        "is_virtual": spec is not None and spec.type == "generic_consumer",
                        "forecast_power_w": None,
                    })

            return {
                "configuration": self.configuration_state(),
                "timestamp": now.isoformat(),
                "planning": self._plan_active_count > 0,
                "grid_power_w": grid_power_w,
                "current_price_eur_per_kwh": current_price,
                "pv_opportunity_price_eur_per_kwh": self._pv_opportunity_price,
                "dry_run": self._dry_run,
                "failsafe": self._failsafe.to_dict(),
                "devices": devices_payload,
                "setpoints": [
                    {
                        "device_id": did,
                        "setpoint_w": sp,
                        "behaviour": b.value if (b := behaviours.get(did)) is not None else None,
                        "mode": mode,
                        "grid_allowed": intent.grid_allowed if intent is not None else None,
                        "export_allowed": intent.export_allowed if intent is not None else None,
                        "category": (
                            dev.category.value
                            if (dev := self._registry.get(did)) is not None
                            else None
                        ),
                    }
                    for did, sp, mode, intent in setpoint_rows
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
        async def get_plan(detail: bool = False) -> dict:
            """Active EnergyPlan: all intents with planned power and mode.

            ``detail=true`` returns every field of each flow (load, storage
            and EV power, stored energy per battery, prices — the plan's
            economics) instead of the slim set the UI needs.
            """
            plan = self._display_plan or self._control_loop._active_plan
            if plan is None:
                return {"created_at": None, "step_minutes": self._optimizer._step_min, "intents": []}
            return _plan_payload(plan, detail)

        def _plan_payload(
            plan: EnergyPlan,
            detail: bool,
            start: datetime | None = None,
            end: datetime | None = None,
        ) -> dict:
            """JSON view of *plan*; flows/intents limited to ``[start, end)``."""
            def keep(ts: datetime) -> bool:
                ts = _as_utc(ts)
                return (start is None or ts >= start) and (end is None or ts < end)

            return {
                "created_at": plan.created_at.isoformat(),
                "step_minutes": plan.step_minutes,
                # Solved site flows (effective PV incl. live floor, grid
                # import/export) — the UI should use these instead of
                # re-deriving import from the raw forecast series.
                "flows": [
                    f.model_dump(mode="json") if detail else {
                        "timestep": f.timestep.isoformat(),
                        "pv_kw": f.pv_kw,
                        "grid_import_kw": f.grid_import_kw,
                        "grid_export_kw": f.grid_export_kw,
                    }
                    for f in plan.flows
                    if keep(f.timestep)
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
                    if keep(i.timestep)
                ],
            }

        @api.get("/api/plan/snapshots")
        async def list_plan_snapshots(start: datetime, end: datetime | None = None) -> dict:
            """Stored plans made in ``[start, end)`` (creation time and whether
            it is the plan made shortly before local midnight for the coming
            day).  Kept as long as
            the measurement history.  Naive times are read as UTC."""
            start = _as_utc(start)
            end = _as_utc(end) if end is not None else datetime.now(timezone.utc)
            return {"snapshots": await self._storage.list_plan_snapshots(start, end)}

        @api.get("/api/plan/snapshot")
        async def get_plan_snapshot(
            at: datetime | None = None,
            day_ahead_date: date | None = None,
            start: datetime | None = None,
            end: datetime | None = None,
            detail: bool = True,
        ) -> dict:
            """A stored plan in the ``/api/plan`` format: the last plan made at
            or before *at*, or the day-ahead plan of the local day
            *day_ahead_date*.  *start*/*end* limit the flows and intents."""
            if (at is None) == (day_ahead_date is None):
                raise HTTPException(status_code=400, detail="give exactly one of at, day_ahead_date")
            if day_ahead_date is not None:
                midnight = datetime(
                    day_ahead_date.year, day_ahead_date.month, day_ahead_date.day
                ).astimezone(timezone.utc)
                plan = await self._storage.load_plan_snapshot(midnight, day_ahead_only=True)
            else:
                plan = await self._storage.load_plan_snapshot(_as_utc(at))
            if plan is None:
                raise HTTPException(status_code=404, detail="no stored plan")
            return _plan_payload(
                plan, detail,
                _as_utc(start) if start is not None else None,
                _as_utc(end) if end is not None else None,
            )

        @api.get("/api/execution")
        async def get_execution(
            start: datetime, end: datetime | None = None, device_ids: str = ""
        ) -> dict:
            """Control execution log in ``[start, end)``: per slot and device
            the dominant intent mode, time-averaged planned power, setpoint
            sent and measured power (W, + = charge/consume)."""
            start = _as_utc(start)
            end = _as_utc(end) if end is not None else datetime.now(timezone.utc)
            if end - start > timedelta(days=8):
                raise HTTPException(status_code=400, detail="window is limited to 8 days")
            ids = [d.strip() for d in device_ids.split(",") if d.strip()]
            return {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "slots": await self._storage.query_execution(start, end, ids or None),
            }

        @api.get("/api/savings")
        async def get_savings(hours: float = 24.0 * 30, bucket: str | None = None) -> dict:
            """Savings against the simulated uncontrolled house over the
            rolling window of the last *hours*, totalled and per *bucket*
            (``hour`` / ``day`` / ``week``, local time; default by window
            length).  Also the energy still stored in both houses and the
            all-time totals.  All values in €."""
            if bucket is None:
                bucket = "hour" if hours <= 72 else "day" if hours <= 24 * 31 else "week"
            if bucket not in _BUCKETS:
                raise HTTPException(status_code=400, detail=f"bucket must be one of {_BUCKETS}")
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=max(0.25, hours))
            all_rows = await self._storage.query_savings_slots(
                datetime.min.replace(tzinfo=UTC), end
            )
            prices, positions = _stored_positions(all_rows, self._savings.batteries)
            first = next(
                (i for i, r in enumerate(all_rows) if r["slot_start"] >= start), len(all_rows)
            )
            rows = all_rows[first:]
            by_bucket: dict[datetime, list[int]] = defaultdict(list)
            for i, row in enumerate(rows, start=first):
                by_bucket[_bucket_start(row["slot_start"], bucket)].append(i)
            last = len(all_rows) - 1
            return {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "bucket": bucket,
                "totals": _stored_change(
                    _savings_totals(rows), first, last if rows else first - 1, positions
                ),
                "buckets": [
                    {
                        "start": b.isoformat(),
                        **_stored_change(
                            _savings_totals([all_rows[i] for i in idx]), idx[0], idx[-1],
                            positions,
                        ),
                    }
                    for b, idx in sorted(by_bucket.items())
                ],
                "stored": self._savings.stored(),
                "reference_price": prices[-1] if prices else None,
                "all_time": _stored_change(_savings_totals(all_rows), 0, last, positions),
                "groups": self._savings_groups(),
                # Per-slot detail (what batteries and cars did in both
                # houses) for windows short enough to chart it.
                "slots": [
                    {**row, "slot_start": row["slot_start"].isoformat()} for row in rows
                ] if hours <= _SLOT_DETAIL_MAX_HOURS else None,
            }

        @api.get("/api/savings/slots")
        async def get_savings_slots(start: datetime, end: datetime | None = None) -> dict:
            """Every savings slot in ``[start, end)``: ledger-based cost of the
            real and the uncontrolled house, both grid bills and the car part
            (€).  Naive times are read as UTC; *end* defaults to now."""
            start = _as_utc(start)
            end = _as_utc(end) if end is not None else datetime.now(timezone.utc)
            if end - start > timedelta(days=31):
                raise HTTPException(status_code=400, detail="window is limited to 31 days")
            all_rows = await self._storage.query_savings_slots(
                datetime.min.replace(tzinfo=UTC), end
            )
            prices, positions = _stored_positions(all_rows, self._savings.batteries)
            idx = [i for i, r in enumerate(all_rows) if r["slot_start"] >= start]
            rows = [all_rows[i] for i in idx]
            return {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "totals": _stored_change(
                    _savings_totals(rows), idx[0] if idx else 0, idx[-1] if idx else -1,
                    positions,
                ),
                "slots": [
                    {
                        **all_rows[i], "slot_start": all_rows[i]["slot_start"].isoformat(),
                        "reference_price": prices[i], "stored_position": positions[i],
                    }
                    for i in idx
                ],
            }

        @api.post("/api/plan/refresh")
        async def trigger_plan_refresh() -> dict:
            """Trigger an immediate plan recomputation outside the normal interval."""
            await self._run_plan()
            plan = self._display_plan or self._control_loop._active_plan
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
            return await self._aligned_forecast()

        @api.get("/api/learned")
        async def get_learned() -> list[dict]:
            """Diagnostics of every learned model: consumption forecasts (model,
            training days, forecast) and thermal models of threshold devices
            (``kind: "thermal"``: parameters, fit quality, measured vs. model)."""
            out = []
            for provider in self._forecast_providers:
                if hasattr(provider, "diagnostics"):
                    out.append(await provider.diagnostics(self._horizon))
            for learner in self._thermal_learners.values():
                out.append(await learner.diagnostics())
            for learner in self._efficiency_learners.values():
                out.append(await learner.diagnostics())
            for item in out:
                item.setdefault("category", _learning_category(item))
            return out

        @api.post("/api/learned/{forecast_id}/refit")
        async def refit_learned(forecast_id: str) -> dict:
            """Refit one learned forecast, thermal or efficiency model now."""
            eff = next(
                (lr for lr in self._efficiency_learners.values() if lr.id == forecast_id), None
            )
            if eff is not None:
                est = await eff.refit()
                if est is None:
                    raise HTTPException(409, f"No efficiency estimate: {eff.diagnostics_reason()}")
                if eff.in_use:
                    asyncio.create_task(self._run_plan())
                return {"ok": True, "fitted_at": est.fitted_at}
            learner = next(
                (lr for lr in self._thermal_learners.values() if lr.id == forecast_id), None
            )
            if learner is not None:
                thermal = await learner.refit()
                if thermal is None:
                    raise HTTPException(
                        409, f"No thermal model fitted: {learner.diagnostics_reason()}"
                    )
                asyncio.create_task(self._run_plan())
                return {"ok": True, "fitted_at": thermal.fitted_at}
            provider = next(
                (p for p in self._forecast_providers
                 if hasattr(p, "diagnostics") and p.forecast_id == forecast_id),
                None,
            )
            if provider is None:
                raise HTTPException(404, f"Unknown learned forecast: {forecast_id!r}")
            try:
                model = await provider.refit()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(502, f"Refit failed: {exc}") from exc
            if model is None:
                raise HTTPException(409, "Not enough usable history to fit a model")
            asyncio.create_task(self._run_plan())
            return {"ok": True, "fitted_at": model.fitted_at}

        @api.get("/api/history")
        async def get_history(
            hours: float = 24.0,
            device_ids: str = "",
            start: datetime | None = None,
            end: datetime | None = None,
            resolution: str = "auto",
        ) -> dict:
            """Device history: mean power and state of charge per bucket.

            Query parameters
            ----------------
            hours:       look-back window (default 24).
            device_ids:  comma-separated device IDs.  Defaults to all storage
                         devices + the meters + PV producers.
            start, end:  explicit window instead of *hours* (naive = UTC; *end*
                         defaults to now).
            resolution:  ``5m``, ``1h`` or ``auto`` (default): 5-minute buckets
                         for windows up to 3 days that the 5-minute tier still
                         covers, hourly buckets beyond.

            Each row is ``{t, power_w, soc_pct, pos_w, neg_w}`` with ``t`` the
            start of the bucket; ``pos_w`` / ``neg_w`` are the means of the
            positive and the negative part of the power (energy in each
            direction = mean x bucket length).
            """
            now = _as_utc(end) if end is not None else datetime.now(timezone.utc)
            begin = _as_utc(start) if start is not None else now - timedelta(hours=max(hours, 0.0))
            fine_from = datetime.now(UTC) - HISTORY_5M_RETENTION
            if resolution == "auto":
                hourly = now - begin > timedelta(days=3) or begin < fine_from
            else:
                hourly = resolution == "1h"

            if device_ids.strip():
                ids = [d.strip() for d in device_ids.split(",") if d.strip()]
            else:
                # Default: storage + meter + PV devices
                ids = [sc.device_id for sc in self._storage_constraints]
                for device in self._registry.by_category(
                    DeviceCategory.METER, DeviceCategory.PV
                ):
                    ids.append(device.device_id)
                ids = list(dict.fromkeys(ids))  # deduplicate preserving order

            def _round(v: float | None) -> float | None:
                return None if v is None else round(v, 1)

            result: dict[str, list] = {}
            for did in ids:
                rows = await self._storage.query_history(did, begin, now, hourly=hourly)
                result[did] = [
                    {
                        "t": r.timestamp.isoformat(),
                        "power_w": _round(r.power_w),
                        "soc_pct": r.soc_pct,
                        "pos_w": _round(r.pos_w),
                        "neg_w": _round(r.neg_w),
                    }
                    for r in rows
                ]

            # Include ledger history for storage devices
            ledger_hist: dict[str, list] = {}
            for sc in self._storage_constraints:
                if device_ids.strip() and sc.device_id not in ids:
                    continue
                ledger_hist[sc.device_id] = await self._storage.query_ledger_history(
                    sc.device_id, begin, now
                )

            return {
                "resolution": "1h" if hourly else "5m",
                "bucket_minutes": 60 if hourly else 5,
                "measurements": result,
                "ledger": ledger_hist,
                **await self._storage.history_bounds(),
            }

        @api.get("/api/config")
        async def get_config() -> dict:
            """Static device configuration: categories, connections + storage parameters."""
            devices = []
            for device in self._registry.all():
                spec = self._cfg.devices.get(device.device_id)
                devices.append({
                    "device_id": device.device_id,
                    "category": device.category.value,
                    "type": spec.type if spec is not None else None,
                    "label": spec.display_name if spec is not None else device.device_id,
                    "capabilities": (
                        sorted(c.value for c in spec.capabilities) if spec is not None else []
                    ),
                    "metered_by": spec.metered_by if spec is not None else None,
                    "controlled_by": spec.controlled_by if spec is not None else None,
                    "included_in": spec.included_in if spec is not None else None,
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
                        # Configured vs. learned round trip, and whether the
                        # plan uses the learned one (plugins/learned/efficiency).
                        "efficiency": (
                            self._efficiency_learners[sc.device_id].summary()
                            if sc.device_id in self._efficiency_learners else None
                        ),
                    }
                    for sc in self._storage_constraints
                ],
            }

        # ── Configuration editor (server/config_api.py) ────────────────
        mount_config_api(api, self)

        # ── EV charging targets ────────────────────────────────────────

        @api.get("/api/vehicles")
        async def get_vehicles() -> list[dict]:
            """Every vehicle: where it is, its SoC, charging goal and plans."""
            device_states = {
                did: state
                for device in self._registry.all()
                if (state := self._registry.latest_state(device.device_id)) is not None
                for did in (device.device_id,)
            }
            # Compute fresh goals so target always reflects the current plan/overrides,
            # not the (potentially stale) last plan run.
            fresh_goals = resolve_active_goals(
                self._ev_assets, device_states,
                self._ev_weekly_plans, self._ev_day_overrides,
                overdue_dismissed=self._ev_overdue_dismissed,
                plugged_since=self._ev_plugged_since,
                feasibility_margin=self._ev_feasibility_margin,
            )
            result = []
            for asset in self._ev_assets:
                # At a charger: the charger's state with the vehicle's SoC;
                # away: the vehicle's own state.
                charger_state = (
                    self._registry.latest_state(asset.device_id) if asset.device_id else None
                )
                vehicle_state = self._registry.latest_state(asset.asset_id)
                state = charger_state or vehicle_state
                vx = vehicle_state.extra if vehicle_state is not None else {}
                charger_spec = self._fleet.chargers.get(asset.device_id)
                goal = next((g for g in fresh_goals if g.asset_id == asset.asset_id), None)
                # phase1/phase2 kWh come from the last optimizer run (more accurate energy split)
                planned = next((g for g in self._last_ev_goals if g.asset_id == asset.asset_id), None)
                force = self._ev_force_charge.get(asset.asset_id)
                result.append({
                    "vehicle_id":     asset.asset_id,
                    # The charger it is at (its plan intents are the
                    # charger's); None while away.
                    "charger_id":     asset.device_id or None,
                    "charger_label":  charger_spec.display_name if charger_spec else None,
                    "session_since":  vx.get("session_since"),
                    "label":          asset.label,
                    "connected":      charger_state.available if charger_state else False,
                    "soc_pct":        state.soc_pct if state else None,
                    "soc_estimated":  bool(vx.get("soc_estimated")),
                    "soc_reported_pct": vx.get("soc_reported_pct"),
                    "soc_reported_at": vx.get("soc_reported_at"),
                    "range_km":       vx.get("range_km"),
                    "car_plugged":    vx.get("car_plugged"),
                    # Its charger: plugged in / selected for the next plug-in /
                    # its default / taken by another car (greyed out in the UI).
                    "charger_status": self._charger_status(
                        self._fleet.sessions.status(asset.asset_id)),
                    "location_home":  vx.get("location_home"),
                    "charge_limit_soc_pct": asset.charge_limit_soc_pct,
                    "max_charge_kw":  asset.max_charge_kw,
                    "goal": {
                        "target_soc_pct":   goal.target_soc_pct,
                        "target_by":        goal.target_by.isoformat(),
                        "phase1_kwh":       round(planned.phase1_required_kwh, 2) if planned else 0.0,
                        "phase2_kwh":       round(planned.phase2_required_kwh, 2) if planned else 0.0,
                        "phase2_start":     planned.phase2_start_time.isoformat() if planned else goal.target_by.isoformat(),
                    } if goal else None,
                    "force_charge": {"target_soc_pct": force} if force is not None else None,
                    "next_days": upcoming_days(
                        asset,
                        self._ev_weekly_plans.get(asset.asset_id, {}),
                        self._ev_day_overrides.get(asset.asset_id, {}),
                    ),
                    "disabled": asset.asset_id in self._disabled_chargepoints,
                })
            return result

        # ── Controllable devices (EV chargers + storage + threshold loads) ──

        @api.get("/api/controllable")
        async def get_controllable() -> dict:
            """Plan data for all controllable devices."""
            plan = self._display_plan or self._control_loop._active_plan
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
                state = (
                    self._registry.latest_state(asset.device_id) if asset.device_id
                    else self._registry.latest_state(asset.asset_id)
                )
                soc_pct = state.soc_pct if state is not None else None
                power_w = state.power_w if state is not None and asset.device_id else None
                connected = state.available if state is not None and asset.device_id else False

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
                    mode = ev_step_mode(intent, ts, phase2_start)
                    planned_kw = intent.power_kw if intent else 0.0
                    if planned_kw > 0:
                        energy_kwh = min(cap_limit, energy_kwh + planned_kw * step_h)
                    steps.append({
                        "ts": ts.isoformat(),
                        "mode": mode,
                        "planned_kw": planned_kw,
                        "soc_pct": round(energy_kwh / asset.capacity_kwh * 100, 1),
                    })

                _active, from_active = _split_active(steps)
                # Same helper as the HA ``charging_state`` sensor.
                current_mode = ev_current_mode(
                    plan, asset.device_id, phase2_start, now,
                    force_charge=asset.asset_id in self._ev_force_charge,
                    disabled=asset.asset_id in self._disabled_chargepoints,
                )

                devices.append({
                    "device_id": asset.asset_id,
                    "vehicle_id": asset.asset_id,
                    "charger_id": asset.device_id or None,
                    "label": asset.label,
                    "category": "vehicle",
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
                    "label": self._label(sc.device_id),
                    "category": "battery",
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
                    "label": tc.label or self._label(tc.device_id),
                    "category": "thermal_load",
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

        def _validate_soc(soc: float) -> None:
            if not (0 <= soc <= 100):
                raise HTTPException(400, "target_soc_pct must be 0–100")

        def _validate_hhmm(s: str) -> str:
            parts = s.split(":")
            try:
                h, m = int(parts[0]), int(parts[1])
                if len(parts) < 2 or not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except (ValueError, IndexError):
                raise HTTPException(400, f"Invalid target_by time: {s!r} (expected HH:MM)")
            return f"{h:02d}:{m:02d}"

        def _parse_date(s: str) -> date:
            try:
                return date.fromisoformat(s)
            except ValueError:
                raise HTTPException(400, f"Invalid date: {s!r} (expected YYYY-MM-DD)")

        @api.get("/api/vehicles/{vehicle_id}/plan")
        async def get_ev_plan(vehicle_id: str) -> dict:
            """Weekly plan, active dated overrides, and the effective next 7 days."""
            asset = _parse_ev_asset(vehicle_id)
            weekly = self._ev_weekly_plans.get(vehicle_id, {})
            overrides = self._ev_day_overrides.get(vehicle_id, {})
            return {
                "vehicle_id": vehicle_id,
                "timezone": asset.timezone,
                "weekly": [
                    {
                        "weekday": wd,
                        "enabled": (wt := default_weekly_target(asset, weekly, wd)).enabled,
                        "target_soc_pct": wt.target_soc_pct,
                        "target_by": wt.target_by,
                    }
                    for wd in range(1, 8)
                ],
                "overrides": [
                    {
                        "date": d.isoformat(),
                        "skip": ov.skip,
                        "target_soc_pct": ov.target_soc_pct,
                        "target_by": ov.target_by,
                    }
                    for d, ov in sorted(overrides.items())
                ],
                "days": upcoming_days(asset, weekly, overrides),
            }

        @api.put("/api/vehicles/{vehicle_id}/plan")
        async def put_ev_weekly_plan(vehicle_id: str, body: WeeklyPlanBody) -> dict:
            """Replace the weekly plan (one row per ISO weekday)."""
            _parse_ev_asset(vehicle_id)
            entries: list[EvWeeklyTarget] = []
            seen: set[int] = set()
            for row in body.weekly:
                if not (1 <= row.weekday <= 7) or row.weekday in seen:
                    raise HTTPException(400, f"Invalid or duplicate weekday: {row.weekday}")
                seen.add(row.weekday)
                _validate_soc(row.target_soc_pct)
                entries.append(EvWeeklyTarget(
                    weekday=row.weekday,
                    enabled=row.enabled,
                    target_soc_pct=row.target_soc_pct,
                    target_by=_validate_hhmm(row.target_by),
                ))
            await self._save_ev_weekly_plan(vehicle_id, entries)
            return {"status": "ok", "vehicle_id": vehicle_id}

        @api.put("/api/vehicles/{vehicle_id}/plan/{day}")
        async def put_ev_day_override(vehicle_id: str, day: str, body: DayOverrideBody) -> dict:
            """Skip or re-target one concrete date. Expires after that local day."""
            asset = _parse_ev_asset(vehicle_id)
            d = _parse_date(day)
            today_local = datetime.now(timezone.utc).astimezone(asset_zoneinfo(asset)).date()
            if d < today_local:
                raise HTTPException(400, f"Date {day} is in the past")
            if body.skip:
                override = EvDayOverride(date=d, skip=True)
            else:
                if body.target_soc_pct is None or body.target_by is None:
                    raise HTTPException(400, "Provide skip=true or target_soc_pct + target_by")
                _validate_soc(body.target_soc_pct)
                override = EvDayOverride(
                    date=d, skip=False,
                    target_soc_pct=body.target_soc_pct,
                    target_by=_validate_hhmm(body.target_by),
                )
            await self._set_ev_day_override(vehicle_id, override)
            _log.info("EV day override: %r %s → %s", vehicle_id, day,
                      "skip" if override.skip else f"{override.target_soc_pct:.0f}% by {override.target_by}")
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "vehicle_id": vehicle_id, "date": day}

        @api.delete("/api/vehicles/{vehicle_id}/plan/{day}")
        async def delete_ev_day_override(vehicle_id: str, day: str) -> dict:
            """Revert one date to the weekly plan."""
            _parse_ev_asset(vehicle_id)
            d = _parse_date(day)
            if d not in self._ev_day_overrides.get(vehicle_id, {}):
                raise HTTPException(404, f"No override for {vehicle_id!r} on {day}")
            await self._clear_ev_day_override(vehicle_id, d)
            _log.info("EV day override cleared: %r %s", vehicle_id, day)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "vehicle_id": vehicle_id, "date": day}

        @api.post("/api/vehicles/{vehicle_id}/force_charge")
        async def force_charge(vehicle_id: str, target_soc_pct: float) -> dict:
            """Charge at full speed until *target_soc_pct*.

            Resets automatically on vehicle unplug or when the target is
            reached.  Only allowed while a vehicle is connected — otherwise
            the flag could never clear.
            """
            asset = _parse_ev_asset(vehicle_id)
            _validate_soc(target_soc_pct)
            if asset.vehicle_id in self._disabled_chargepoints:
                raise HTTPException(409, f"Chargepoint {vehicle_id!r} is disabled")
            state = self._registry.latest_state(asset.device_id)
            if state is None or not state.available:
                raise HTTPException(409, f"No vehicle connected at {vehicle_id!r}")
            await self._apply_force_charge(vehicle_id, target_soc_pct)
            return {"status": "ok", "vehicle_id": vehicle_id, "target_soc_pct": target_soc_pct}

        @api.delete("/api/vehicles/{vehicle_id}/force_charge")
        async def cancel_force_charge(vehicle_id: str) -> dict:
            """Cancel an active force charge (control returns to the plan)."""
            _parse_ev_asset(vehicle_id)
            if vehicle_id not in self._ev_force_charge:
                raise HTTPException(404, f"No active force charge for {vehicle_id!r}")
            await self._clear_force_charge(vehicle_id, "cancelled via API")
            return {"status": "ok", "vehicle_id": vehicle_id}

        @api.post("/api/vehicles/{vehicle_id}/disable")
        async def disable_chargepoint(vehicle_id: str) -> dict:
            """Exclude this chargepoint from optimizer and control."""
            if not any(a.vehicle_id == vehicle_id for a in self._ev_assets):
                raise HTTPException(404, f"Unknown EV asset: {vehicle_id!r}")
            await self._storage.set_ev_disabled(vehicle_id)
            self._disabled_chargepoints.add(vehicle_id)
            self._fleet.apply(self._disabled_chargepoints, self._ev_force_charge)
            _log.info("Chargepoint disabled: %r", vehicle_id)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "vehicle_id": vehicle_id, "disabled": True}

        @api.delete("/api/vehicles/{vehicle_id}/disable")
        async def enable_chargepoint(vehicle_id: str) -> dict:
            """Re-include this chargepoint in optimizer and control."""
            if not any(a.vehicle_id == vehicle_id for a in self._ev_assets):
                raise HTTPException(404, f"Unknown EV asset: {vehicle_id!r}")
            await self._storage.clear_ev_disabled(vehicle_id)
            self._disabled_chargepoints.discard(vehicle_id)
            self._fleet.apply(self._disabled_chargepoints, self._ev_force_charge)
            _log.info("Chargepoint enabled: %r", vehicle_id)
            asyncio.create_task(self._run_plan())
            return {"status": "ok", "vehicle_id": vehicle_id, "disabled": False}

        # ── Chargers and charging sessions ──────────────────────────────

        @api.get("/api/chargers")
        async def get_chargers() -> list[dict]:
            """Every charger with the car at it (vehicle, guest or none) and the
            vehicles it can be assigned to."""
            out = []
            for cid, spec in self._fleet.chargers.items():
                session = self._fleet.session(cid)
                state = self._registry.latest_state(cid)
                out.append({
                    "charger_id": cid,
                    "label": spec.display_name,
                    "plugged": bool(state.extra.get("plugged")) if state else False,
                    "power_w": state.power_w if state else None,
                    "default_vehicle": spec.params.get("default_vehicle"),
                    # Pre-selected for the next plug-in: a vehicle id, "guest",
                    # or None (the default applies).
                    "next_vehicle": self._fleet.sessions.next_vehicle.get(cid),
                    # How a guest car charges here: chosen for this guest, else configured.
                    "guest_mode": self._fleet.guest_mode(cid),
                    "session": {
                        "vehicle_id": session.vehicle_id,
                        "guest": session.is_guest,
                        "since": session.started_at.isoformat(),
                        "source": session.source,
                    } if session is not None else None,
                    "vehicles": [
                        {"vehicle_id": vid, "label": v.display_name}
                        for vid, v in self._fleet.vehicles.items()
                    ],
                })
            return out

        @api.put("/api/guest/mode")
        async def put_guest_mode(mode: str, charger_id: str | None = None) -> dict:
            """How the guest car charges: ``pv_only`` (PV surplus) or ``instant``
            (full power now).  At *charger_id*, or wherever the guest is.
            Back to the charger's configured mode when the guest leaves."""
            try:
                await self.set_guest_mode(charger_id, mode)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return {"status": "ok", "mode": mode}

        @api.put("/api/vehicles/{vehicle_id}/charger")
        async def put_ev_charger(vehicle_id: str, charger_id: str) -> dict:
            """Put a car (or ``guest``) on a charger — the last selection wins.
            A car plugged in there is reassigned now; a free charger expects
            this car at its next plug-in.  ``charger_id=none`` drops the
            car's selections."""
            try:
                await self.select_charger(
                    None if vehicle_id == "guest" else vehicle_id,
                    None if charger_id == "none" else charger_id,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return {"status": "ok", "vehicle_id": vehicle_id, "charger_id": charger_id}

        @api.get("/api/sessions")
        async def get_sessions(start: datetime, end: datetime | None = None) -> dict:
            """Charging sessions overlapping ``[start, end)`` (default end: now)."""
            end_ = _as_utc(end) if end is not None else datetime.now(UTC)
            return {"sessions": await self._storage.query_vehicle_sessions(_as_utc(start), end_)}

        # ── Static frontend (registered last so API routes take priority) ──────
        # The web UI is built with Vite (``npm run build`` in ``frontend/``) into
        # ``energy_assistant/frontend/`` inside this package, which is where both
        # a dev checkout and an installed wheel find it.  index.html is served at
        # ``/`` and refers to everything else by relative URL, so the UI works
        # unchanged behind Home Assistant ingress.
        _fe = Path(__file__).resolve().parent.parent / "frontend"
        if (_fe / "index.html").exists():
            from fastapi.responses import FileResponse
            from starlette.middleware.base import BaseHTTPMiddleware

            class _UiCacheMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    response = await call_next(request)
                    if request.url.path.startswith("/assets/"):
                        # Built files carry a content hash in their name.
                        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                    return response

            api.add_middleware(_UiCacheMiddleware)
            api.mount("/assets", StaticFiles(directory=str(_fe / "assets")), name="assets")

            @api.get("/")
            async def serve_ui() -> FileResponse:
                # Never cached: it names the current build's files.
                return FileResponse(str(_fe / "index.html"), headers={"Cache-Control": "no-store"})

            @api.get("/favicon.svg", include_in_schema=False)
            async def serve_favicon() -> FileResponse:
                return FileResponse(str(_fe / "favicon.svg"))
        else:
            @api.get("/", response_class=HTMLResponse)
            async def serve_ui_missing() -> str:
                return (
                    "<h1>The web UI is not built.</h1>"
                    "<p>Run <code>npm ci &amp;&amp; npm run build</code> in <code>frontend/</code> "
                    "and restart Energy Assistant. The API works without it.</p>"
                )

        # Read-only MCP endpoint (POST /mcp) for debugging tools.
        mcp_holder.append(mount_mcp_server(api))

        # Outermost: guards everything not coming through HA ingress.
        token = os.environ.get("ENERGY_ASSISTANT_API_TOKEN") or self._cfg.server.get("api_token")
        api.add_middleware(
            ApiAccessMiddleware,
            token=str(token) if token else None,
            trusted_hosts=self._cfg.server.get("trusted_hosts") or DEFAULT_TRUSTED_HOSTS,
        )
        if token:
            _log.info("API: remote access needs the bearer token (read-only)")

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

    def _label(self, device_id: str) -> str:
        """The device's display name (its label, or its id title-cased)."""
        spec = self._cfg.devices.get(device_id)
        return spec.display_name if spec is not None else device_id.replace("_", " ").title()

    async def _report_orphaned_data(self) -> None:
        """Log stored rows whose id is no configured device of the right
        category (left over from renamed or removed devices).  Nothing is
        deleted; the warning names the table and the ids."""
        stored_ids = getattr(self._storage, "stored_ids", None)
        if stored_ids is None:
            return
        try:
            stored = await stored_ids()
        except Exception as exc:  # noqa: BLE001 — a diagnostic only
            _log.debug("orphan check failed: %s", exc)
            return
        batteries = {sc.device_id for sc in self._storage_constraints}
        vehicles, chargers = set(self._fleet.vehicles), set(self._fleet.chargers)
        expected = {
            "ledger_state": batteries,
            "ev_weekly_plan": vehicles, "ev_day_overrides": vehicles,
            "ev_force_charge": vehicles, "ev_disabled": vehicles,
            "vehicle_sessions": chargers, "charger_next_vehicle": chargers,
            "charger_guest_mode": chargers,
        }
        for table, ids in sorted(stored.items()):
            orphans = sorted(ids - expected.get(table, ids))
            if orphans:
                _log.warning("Stored data without a matching device: %s: %s",
                             table, ", ".join(orphans))

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

    def _build_tariff_zones(self) -> dict[str, _TariffZone]:
        """Build per-zone circuit maps driven by the topology tree.

        The topology is the authoritative source for zone meters: only
        ``meter`` devices that appear as *non-root* topology nodes become
        zone meters.  Their subtrees define which ``pv`` and ``battery``
        devices belong to that zone.

        Any ``pv`` or ``battery`` device that does not appear in the topology
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

        # Fallback: pv / battery devices not captured by topology subtrees
        for device in self._registry.all():
            if device.device_id in claimed:
                continue
            tariff_id = self._device_tariff(device.device_id) or default_tariff
            if not tariff_id:
                continue
            zone = zones.setdefault(tariff_id, _TariffZone(tariff_id))
            if device.category is DeviceCategory.PV:
                zone.producer_ids.append(device.device_id)
            elif device.category is DeviceCategory.BATTERY:
                zone.storage_ids.append(device.device_id)

        # Differential consumers: heatpump = Z1 − Z2.
        # Build a zone that knows its minuend (Z1) and subtrahend (Z2) so
        # _compute_zone_market_prices can blend the flat grid rate with any
        # Z2 feedback (household PV/battery surplus flowing into heatpump).
        for device in self._registry.by_category(DeviceCategory.CONSUMER):
            spec = self._cfg.devices.get(device.device_id)
            if spec is None or spec.type != "differential":
                continue
            tariff_id = spec.tariff or default_tariff
            if not tariff_id:
                continue
            minuend_id = spec.settings.get("minuend")
            subtrahend_id = spec.settings.get("subtrahend")
            if not minuend_id or not subtrahend_id:
                continue
            zone = zones.setdefault(tariff_id, _TariffZone(tariff_id))
            zone.diff_minuend_id = minuend_id
            zone.diff_subtrahend_id = subtrahend_id

        return zones

    def _device_tariff(self, device_id: str) -> str | None:
        """The ``tariff:`` configured on a device, if any."""
        spec = self._cfg.devices.get(device_id)
        return spec.tariff if spec is not None else None

    def _collect_zone_from_node(
        self,
        node: TopologyNode,
        zones: dict[str, _TariffZone],
        claimed: set[str],
        default_tariff: str,
    ) -> None:
        """Recursively walk a topology sub-tree and assign devices to zones.

        A ``meter`` node defines the zone: it becomes that zone's grid
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

        if device.category is DeviceCategory.METER:
            tariff_id = self._device_tariff(device.device_id) or default_tariff
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
        zone: _TariffZone,
        claimed: set[str],
    ) -> None:
        """Add all PRODUCER/STORAGE devices in *node*'s subtree to *zone*.

        Stops recursing into nested ``meter`` nodes — those define their own zones.
        """
        device = self._registry.get(node.device_id)
        claimed.add(node.device_id)
        if device is not None:
            if device.category is DeviceCategory.PV:
                zone.producer_ids.append(device.device_id)
            elif device.category is DeviceCategory.BATTERY:
                zone.storage_ids.append(device.device_id)
            elif device.category is DeviceCategory.METER:
                # Nested meter — will be its own zone; don't claim for parent
                claimed.discard(node.device_id)
                return
        for child in node.children:
            self._collect_subtree_for_zone(child, zone, claimed)

    def _compute_zone_market_prices(
        self,
        tariff_prices: dict[str, float],
        device_states: dict[str, Any],
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
        device_states: dict[str, Any],
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
            pv_price = self._zone_pv_opportunity_price(tariff_id, zone, tariff_prices, device_states)
            price = (
                (pv_w  / total_w) * pv_price
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

    def _zone_pv_opportunity_price(
        self,
        tariff_id: str,
        zone: _TariffZone,
        tariff_prices: dict[str, float],
        device_states: dict[str, Any],
    ) -> float:
        """Opportunity price of PV used in *zone* (see ``surplus_opportunity_price``).

        Only the default zone's surplus feeds the separately billed loads;
        every other zone keeps the export price.
        """
        export_price = self._pv_opportunity_price
        if tariff_id != (self._cfg.default_tariff_id or ""):
            return export_price
        secondary_price = self._secondary_import_price(tariff_prices, device_states)
        zone_grid_w = self._default_zone_grid_power_w(device_states)
        site = device_states.get(self._grid_meter_id) if self._grid_meter_id else None
        if (
            secondary_price is None
            or zone_grid_w is None
            or site is None
            or site.power_w is None
        ):
            return export_price
        charge_w = sum(
            s.power_w
            for did in zone.storage_ids
            if (s := device_states.get(did)) is not None
            and s.power_w is not None
            and s.power_w > 0.0
        )
        return surplus_opportunity_price(
            site_grid_w=site.power_w,
            zone_grid_w=zone_grid_w,
            storage_charge_w=charge_w,
            export_price=export_price,
            secondary_price=secondary_price,
        )

    def _default_zone_grid_power_w(self, device_states: dict[str, Any]) -> float | None:
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

    async def _aligned_forecast(self) -> dict:
        """Last forecast snapshot aligned to the active plan timesteps.

        Served by ``/api/forecast`` and used for the MQTT price curve.
        """
        plan = self._display_plan or self._control_loop._active_plan
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
                for sc in self._planning_storage_constraints()
            },
        }

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

    async def _build_tariff_zone_inputs(
        self,
    ) -> tuple[list[ForecastPoint], dict[datetime, bool], list[SecondaryTariffLoad]]:
        """Return the optimizer's price inputs, split by metering zone.

        * the default tariff's import price (with its estimated-source flags),
          which is what every flexible device — batteries, EV chargers — pays
          for an extra kWh, and
        * one ``SecondaryTariffLoad`` per other tariff that has consumers with
          a forecast (e.g. a heat pump on a flat heat-pump tariff).

        Earlier versions blended all tariffs into a single price weighted by
        forecast load.  That made cheap flat-rate load drag the price down in
        exactly the hours it was largest, so flexible devices were scheduled
        against a price they never pay.

        Returns ``([], {}, [])`` when the default tariff has no usable price
        schedule; the caller then falls back to any tariff with prices.
        """
        primary_id = self._cfg.default_tariff_id
        primary = self._tariffs.get(primary_id) if primary_id else None
        if primary is None:
            return [], {}, []
        try:
            sched = await primary.price_schedule(self._horizon)
        except Exception as exc:  # noqa: BLE001
            _log.debug("Default tariff %r has no price schedule: %s", primary_id, exc)
            return [], {}, []
        if not sched or not any(tp.price_eur_per_kwh > 0.001 for tp in sched):
            return [], {}, []
        ext, est_ts = _extend_tariff_schedule_with_daily_repeat(sched, self._horizon)
        primary_prices = [
            ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh) for tp in ext
        ]
        estimated_by_ts = {tp.timestamp: (tp.timestamp in est_ts) for tp in ext}

        # Forecast load of consumers billed on any other tariff, per tariff.
        load_by_tariff: dict[str, dict[datetime, float]] = defaultdict(dict)
        forecast_cfgs = effective_forecast_configs(self._cfg)
        for device_id, spec in self._cfg.devices.items():
            device = self._registry.get(device_id)
            if device is None or device.category is not DeviceCategory.CONSUMER:
                continue
            fc_cfg = forecast_cfgs.get(device_id)
            if fc_cfg is None:
                continue
            tariff_id = spec.tariff or primary_id
            if tariff_id == primary_id or tariff_id not in self._tariffs:
                continue

            try:
                # Reuse the provider planning already uses; building a fresh
                # one per plan would refit stateful (learned) forecasts.
                provider = getattr(self, "_device_forecast_providers", {}).get(device_id)
                if provider is None:
                    provider = plugin_registry.build_forecast(
                        f"{device_id}_forecast", fc_cfg, self._build_ctx
                    )
                if provider is None or provider.quantity != ForecastQuantity.CONSUMPTION:
                    continue
                pts = await provider.get_forecast(self._horizon)
            except Exception as exc:  # noqa: BLE001
                _log.debug("Could not build forecast for %s: %s", device_id, exc)
                continue

            bucket = load_by_tariff[tariff_id]
            for pt in pts:
                bucket[pt.timestamp] = bucket.get(pt.timestamp, 0.0) + float(pt.value)

        secondary: list[SecondaryTariffLoad] = []
        for tariff_id, loads in load_by_tariff.items():
            if not loads:
                continue
            try:
                t_sched = await self._tariffs[tariff_id].price_schedule(self._horizon)
            except Exception:  # noqa: BLE001
                t_sched = None
            if not t_sched:
                # No price for this tariff: its load stays in the total
                # consumption and is billed at the default price.
                continue
            t_ext, _ = _extend_tariff_schedule_with_daily_repeat(t_sched, self._horizon)
            secondary.append(
                SecondaryTariffLoad(
                    tariff_id=tariff_id,
                    load=[
                        ForecastPoint(timestamp=ts, value=kw)
                        for ts, kw in sorted(loads.items())
                    ],
                    prices=[
                        ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh)
                        for tp in t_ext
                    ],
                )
            )
        return primary_prices, estimated_by_ts, secondary

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

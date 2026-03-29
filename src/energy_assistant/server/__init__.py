"""Application — main orchestrator for the Energy Assistant platform.

Wires all platform components together and runs three concurrent async loops:

Polling loop (``control_interval_s``, default 30 s)
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
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from ..config.yaml import YamlConfigLoader
from ..core.config import AppConfig
from ..core.control import ControlLoop, LiveSituation, StorageControlContributor
from ..core.event import DeviceStateEvent, EventBus, PlanUpdatedEvent
from ..core.forecast import ForecastProvider
from ..core.ledger import BatteryCostLedger
from ..core.models import (
    ForecastPoint,
    ForecastQuantity,
    Measurement,
    StorageConstraints,
)
from ..core.optimizer import OptimizationContext
from ..core.registry import DeviceRegistry
from ..core.tariff import TariffModel
from ..core.topology import TopologyNode
from ..loader.device_loader import build, build_all_forecasts, make_build_context
from ..plugins.milp_highs import MilpHigsOptimizer
from ..storage.sqlite import SqliteStorageBackend

_log = logging.getLogger(__name__)


def _web_ui_html() -> str:
        """Return a lightweight built-in web UI for live diagnostics."""
        return """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Energy Assistant UI</title>
    <script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\"></script>
    <style>
        :root {
            --bg: #f6f8f4;
            --card: #ffffff;
            --ink: #1f2a22;
            --muted: #5d6b61;
            --ok: #1d7f4e;
            --warn: #ad7b00;
            --bad: #9f2d2d;
            --line: #d7ddd8;
            --accent: #0f6a8f;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: ui-sans-serif, -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
            background: radial-gradient(circle at top right, #e7efe8, var(--bg));
            color: var(--ink);
        }
        .wrap {
            max-width: 1400px;
            margin: 0 auto;
            padding: 16px;
        }
        .top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            gap: 12px;
            flex-wrap: wrap;
        }
        h1 {
            font-size: 1.2rem;
            margin: 0;
            letter-spacing: 0.02em;
        }
        .stamp { color: var(--muted); font-size: 0.9rem; }
        .grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(180px, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }
        .card {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 10px 12px;
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }
        .k { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: .08em; }
        .v { font-size: 1.35rem; font-weight: 700; margin-top: 4px; }
        .ok { color: var(--ok); }
        .warn { color: var(--warn); }
        .bad { color: var(--bad); }
        .section {
            display: grid;
            grid-template-columns: 1.3fr 1fr;
            gap: 12px;
            margin-bottom: 12px;
        }
        .panel {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 10px;
        }
        .panel h2 {
            margin: 0 0 8px 0;
            font-size: 0.95rem;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: .08em;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
        }
        th, td { padding: 6px; border-bottom: 1px solid var(--line); text-align: left; }
        th { color: var(--muted); font-weight: 600; }
        @media (max-width: 980px) {
            .grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
            .section { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"top\">
            <h1>Energy Assistant Live UI</h1>
            <div class=\"stamp\" id=\"stamp\">loading...</div>
        </div>

        <div class=\"grid\">
            <div class=\"card\"><div class=\"k\">Grid Power</div><div class=\"v\" id=\"gridPower\">-</div></div>
            <div class=\"card\"><div class=\"k\">Import Price</div><div class=\"v\" id=\"importPrice\">-</div></div>
            <div class=\"card\"><div class=\"k\">Export Price</div><div class=\"v\" id=\"exportPrice\">-</div></div>
            <div class=\"card\"><div class=\"k\">Dry Run</div><div class=\"v\" id=\"dryRun\">-</div></div>
        </div>

        <div class=\"section\">
            <div class=\"panel\">
                <h2>Plan Power (kW)</h2>
                <div id=\"planChart\" style=\"height:360px\"></div>
            </div>
            <div class=\"panel\">
                <h2>Active Setpoints</h2>
                <table id=\"setpointsTable\">
                    <thead><tr><th>Device</th><th>Mode</th><th>Policy</th><th>Setpoint W</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>

        <div class=\"section\">
            <div class=\"panel\">
                <h2>Devices</h2>
                <table id=\"devicesTable\">
                    <thead><tr><th>Device</th><th>Power W</th><th>SoC %</th><th>OK</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>
            <div class=\"panel\">
                <h2>Battery Ledger</h2>
                <table id=\"ledgerTable\">
                    <thead><tr><th>Device</th><th>Stored kWh</th><th>Basis €/kWh</th><th>Cap kWh</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        function fmt(n, d=2) {
            if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
            return Number(n).toFixed(d);
        }

        function setText(id, txt, cls='') {
            const el = document.getElementById(id);
            el.textContent = txt;
            el.className = 'v ' + cls;
        }

        function tableRows(id, rows) {
            const tbody = document.querySelector('#' + id + ' tbody');
            tbody.innerHTML = rows.map(r => '<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>').join('');
        }

        function buildPlanChart(plan) {
            const intents = plan.intents || [];
            const byDevice = {};
            for (const i of intents) {
                const dev = i.device_id;
                if (!byDevice[dev]) byDevice[dev] = {x: [], y: []};
                byDevice[dev].x.push(i.timestep);
                byDevice[dev].y.push(Number(i.planned_kw || 0));
            }
            const traces = Object.entries(byDevice).map(([dev, arr]) => ({
                type: 'scatter', mode: 'lines', name: dev, x: arr.x, y: arr.y,
            }));
            Plotly.newPlot('planChart', traces, {
                margin: {l: 50, r: 10, t: 10, b: 40},
                yaxis: {title: 'kW'},
                xaxis: {title: 'time'},
                legend: {orientation: 'h'},
                paper_bgcolor: 'white',
                plot_bgcolor: 'white',
            }, {responsive: true, displayModeBar: false});
        }

        function activePolicies(plan, nowIso) {
            const now = new Date(nowIso).getTime();
            const map = {};
            for (const i of (plan.intents || [])) {
                const ts = new Date(i.timestep).getTime();
                if (ts <= now) {
                    const cur = map[i.device_id];
                    if (!cur || ts > cur.ts) {
                        map[i.device_id] = {
                            ts,
                            cp: i.charge_policy || 'auto',
                            dp: i.discharge_policy || 'meet_load_only',
                        };
                    }
                }
            }
            return map;
        }

        async function refresh() {
            const [statusResp, planResp] = await Promise.all([
                fetch('/api/status'),
                fetch('/api/plan'),
            ]);
            const status = await statusResp.json();
            const plan = await planResp.json();

            const gp = Number(status.grid_power_w || 0);
            const gpClass = gp > 50 ? 'warn' : (gp < -50 ? 'ok' : '');
            setText('gridPower', `${Math.round(gp)} W`, gpClass);
            setText('importPrice', `${fmt(status.current_price_eur_per_kwh, 4)} €/kWh`);
            setText('exportPrice', `${fmt(status.pv_opportunity_price_eur_per_kwh, 4)} €/kWh`);
            setText('dryRun', status.dry_run ? 'YES' : 'NO', status.dry_run ? 'warn' : 'ok');
            document.getElementById('stamp').textContent = `updated ${new Date(status.timestamp).toLocaleString()}`;

            const policies = activePolicies(plan, status.timestamp);

            tableRows('setpointsTable', (status.setpoints || []).map(sp => {
                const p = policies[sp.device_id] || {cp: 'auto', dp: 'meet_load_only'};
                return [
                    sp.device_id,
                    sp.mode || '-',
                    `c=${p.cp}, d=${p.dp}`,
                    String(Math.round(Number(sp.setpoint_w || 0))),
                ];
            }));

            tableRows('devicesTable', (status.devices || []).map(d => [
                d.device_id,
                String(Math.round(Number(d.power_w || 0))),
                d.soc_pct === null || d.soc_pct === undefined ? '-' : fmt(d.soc_pct, 1),
                d.available ? 'yes' : 'no',
            ]));

            tableRows('ledgerTable', (status.ledger || []).map(l => [
                l.device_id,
                fmt(l.stored_energy_kwh, 2),
                fmt(l.cost_basis_eur_per_kwh, 4),
                fmt(l.capacity_kwh, 2),
            ]));

            buildPlanChart(plan);
        }

        refresh().catch(console.error);
        setInterval(() => refresh().catch(console.error), 10000);
    </script>
</body>
</html>
"""

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
    """Cap the planning horizon at the latest timestamp in live data forecasts.

    Only PRICE and PV_GENERATION forecasts are used as limits — CONSUMPTION
    is typically a static profile that extends indefinitely, so including it
    would not reflect actual data availability.  This prevents the optimizer
    from seeing a price array where the tail is padded with the last-known
    value (nearest-neighbour artefact), which corrupts the p70 terminal-value
    calculation.
    """
    now = datetime.now(timezone.utc)
    latest = now
    for quantity in (ForecastQuantity.PRICE, ForecastQuantity.PV_GENERATION):
        pts = forecasts.get(quantity, [])
        if pts:
            candidate = max(p.timestamp for p in pts)
            if candidate > latest:
                latest = candidate
    raw_delta = latest - now
    capped = min(raw_delta, cap)
    step_td = timedelta(minutes=step_minutes)
    n_steps = max(1, int(capped.total_seconds() / step_td.total_seconds()))
    return step_td * n_steps


async def _collect_forecasts(
    providers: list[ForecastProvider],
    horizon: timedelta,
) -> dict[ForecastQuantity, list[ForecastPoint]]:
    """Call every provider and group points by quantity.

    Multiple providers for the same quantity (e.g. several consumption
    profiles for different devices) have their point lists concatenated.
    The optimizer's nearest-neighbour interpolation then effectively sums
    them per timestamp.
    """
    result: dict[ForecastQuantity, list[ForecastPoint]] = {}
    for provider in providers:
        try:
            pts = await provider.get_forecast(horizon)
            q = provider.quantity
            if q in result:
                result[q].extend(pts)
            else:
                result[q] = list(pts)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Forecast provider %r failed: %s", getattr(provider, "quantity", "?"), exc)
    return result


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
        self._plan_interval_s: float
        self._control_interval_s: float
        self._dry_run: bool
        self._first_poll_done: asyncio.Event
        self._api: FastAPI

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
        self._dry_run = bool(ctl.get("dry_run", False))
        horizon_h = int(opt.get("horizon_hours", 24))
        self._horizon = timedelta(hours=horizon_h)

        # 2 — Build devices / tariffs / topology (shared connection pool)
        ctx = make_build_context(self._cfg)
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
            step_minutes=int(opt.get("step_minutes", 60))
        )

        # 7 — Ledger + control loop
        self._ledger = BatteryCostLedger()
        self._control_loop = ControlLoop(ledger=self._ledger)
        for sc in self._storage_constraints:
            self._control_loop.register_contributor(StorageControlContributor(sc))
        _log.info("Registered %d storage contributors", len(self._storage_constraints))

        # 8 — Subscribe control loop to plan updates via event bus
        async def _on_plan_updated(event: PlanUpdatedEvent) -> None:
            self._control_loop.update_plan(event.plan)

        self._bus.subscribe(PlanUpdatedEvent, _on_plan_updated)

        # 9 — Resolve helper lookups
        self._default_tariff = (
            self._tariffs.get(self._cfg.default_tariff_id)
            if self._cfg.default_tariff_id
            else (next(iter(self._tariffs.values()), None))
        )
        self._grid_meter_id = self._topology.device_id if self._topology else None
        self._pv_opportunity_price = 0.0  # refreshed on first planning cycle
        self._first_poll_done = asyncio.Event()

        if self._dry_run:
            _log.warning("DRY RUN — control commands will be logged but not sent")

        # 10 — Build API + launch loops
        self._api = self._build_api()
        port = int(self._cfg.server.get("port", 8088))
        _log.info("API listening on http://0.0.0.0:%d", port)
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
    # Polling loop
    # ------------------------------------------------------------------

    async def _polling_loop(self) -> None:
        """Read every device, persist to SQLite, publish DeviceStateEvents."""
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

            await asyncio.sleep(self._control_interval_s)

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

        forecasts = await _collect_forecasts(self._forecast_providers, self._horizon)

        # Cap the effective horizon at the latest data point actually available.
        # Without this, price arrays are padded with repeated last-known values
        # (nearest-neighbour artefact), which corrupts the p70 terminal-value calc.
        effective_horizon = _infer_effective_horizon(
            forecasts, self._optimizer._step_min, self._horizon
        )

        context = OptimizationContext(
            device_states=device_states,
            storage_constraints=self._storage_constraints,
            tariffs=self._tariffs,
            forecasts=forecasts,
            horizon=effective_horizon,
            battery_cost_basis=self._ledger.all_cost_bases(),
        )

        try:
            plan = await self._optimizer.optimize(context)
        except Exception as exc:  # noqa: BLE001
            _log.error("Optimizer failed: %s", exc)
            return

        _log.info("New plan: %d intents  horizon=%s  (cap=%s)", len(plan.intents), effective_horizon, self._horizon)
        await self._bus.publish(PlanUpdatedEvent(plan=plan))
        await self._bus.flush()

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
        )

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

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    def _build_api(self) -> FastAPI:
        """Build the FastAPI application exposing live server state."""
        api = FastAPI(title="Energy Assistant", version="0.1")

        @api.get("/", response_class=HTMLResponse)
        async def ui_root() -> str:
            """Built-in live web UI with Plotly charts."""
            return _web_ui_html()

        @api.get("/ui", response_class=HTMLResponse)
        async def ui_page() -> str:
            """Alias for the built-in live web UI."""
            return _web_ui_html()

        @api.get("/health")
        async def health() -> dict:
            """Liveness probe endpoint used by container health checks."""
            return {"status": "ok"}

        @api.get("/api/status")
        async def get_status() -> dict:
            """Live snapshot: grid power, price, device states, setpoints, ledger."""
            now = datetime.now(timezone.utc)

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

            live = LiveSituation(
                timestamp=now,
                grid_power_w=grid_power_w,
                dt_hours=0.0,
                device_states=device_states_map,
                current_price_eur_per_kwh=current_price,
                pv_opportunity_price_eur_per_kwh=self._pv_opportunity_price,
            )

            return {
                "timestamp": now.isoformat(),
                "grid_power_w": grid_power_w,
                "current_price_eur_per_kwh": current_price,
                "pv_opportunity_price_eur_per_kwh": self._pv_opportunity_price,
                "dry_run": self._dry_run,
                "devices": [
                    {
                        "device_id": s.device_id,
                        "power_w": s.power_w,
                        "soc_pct": s.soc_pct,
                        "available": s.available,
                        "timestamp": s.timestamp.isoformat(),
                    }
                    for s in device_states_map.values()
                ],
                "setpoints": [
                    {"device_id": did, "setpoint_w": sp, "mode": mode}
                    for did, sp, mode in self._control_loop.describe_setpoints(live)
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
            }

        @api.get("/api/plan")
        async def get_plan() -> dict:
            """Active EnergyPlan: all intents with planned power and mode."""
            plan = self._control_loop._active_plan
            if plan is None:
                return {"created_at": None, "step_minutes": self._optimizer._step_min, "intents": []}
            return {
                "created_at": plan.created_at.isoformat(),
                "step_minutes": self._optimizer._step_min,
                "intents": [
                    {
                        "device_id": i.device_id,
                        "timestep": i.timestep.isoformat(),
                        "mode": i.mode,
                        "charge_policy": i.charge_policy,
                        "discharge_policy": i.discharge_policy,
                        "planned_kw": i.planned_kw,
                        "min_power_w": i.min_power_w,
                        "max_power_w": i.max_power_w,
                        "reserved_kwh": i.reserved_kwh,
                    }
                    for i in plan.intents
                ],
            }

        @api.get("/api/ledger")
        async def get_ledger() -> list:
            """Battery cost basis and stored energy from the live ledger."""
            return [
                {
                    "device_id": sc.device_id,
                    "cost_basis_eur_per_kwh": self._ledger.cost_basis(sc.device_id),
                    "stored_energy_kwh": self._ledger.stored_energy(sc.device_id),
                    "capacity_kwh": sc.capacity_kwh,
                }
                for sc in self._storage_constraints
            ]

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

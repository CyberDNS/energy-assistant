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
    DeviceRole,
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
    """Return the built-in multi-tab web UI for live diagnostics."""
    return """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Energy Assistant</title>
    <script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\"></script>
    <style>
        :root {
            --bg: #f3f5f2;
            --card: #ffffff;
            --ink: #1a2420;
            --muted: #586660;
            --ok: #1d7f4e;
            --warn: #ad7b00;
            --bad: #9f2d2d;
            --line: #d5dbd6;
            --accent: #0f6a8f;
            --tab-active: #0f6a8f;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: ui-sans-serif, -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--ink);
            font-size: 14px;
        }
        .wrap { max-width: 1500px; margin: 0 auto; padding: 14px 16px; }
        /* ── header ── */
        .top { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px; }
        h1 { font-size:1.15rem; letter-spacing:.03em; }
        .stamp { color:var(--muted); font-size:.82rem; }
        /* ── KPI bar ── */
        .kpis { display:grid; grid-template-columns:repeat(auto-fill,minmax(145px,1fr)); gap:8px; margin-bottom:12px; }
        .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:8px 12px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
        .k { color:var(--muted); font-size:.73rem; text-transform:uppercase; letter-spacing:.07em; }
        .v { font-size:1.25rem; font-weight:700; margin-top:3px; }
        .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
        /* ── tabs ── */
        .tab-nav { display:flex; gap:4px; margin-bottom:12px; border-bottom:2px solid var(--line); padding-bottom:0; }
        .tab-btn {
            padding:7px 18px; border:none; background:none; cursor:pointer;
            color:var(--muted); font-size:.9rem; font-weight:600; border-radius:6px 6px 0 0;
            border-bottom:3px solid transparent; margin-bottom:-2px; transition:color .15s;
        }
        .tab-btn.active { color:var(--tab-active); border-bottom-color:var(--tab-active); }
        .tab-btn:hover:not(.active) { color:var(--ink); background:var(--line); }
        .tab-pane { display:none; }
        .tab-pane.active { display:block; }
        /* ── layout helpers ── */
        .row2 { display:grid; grid-template-columns:1.4fr 1fr; gap:10px; margin-bottom:10px; }
        .row2-eq { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px; }
        .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; margin-bottom:10px; }
        .full { margin-bottom:10px; }
        .panel { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
        .panel h2 { font-size:.78rem; color:var(--muted); text-transform:uppercase; letter-spacing:.07em; margin-bottom:8px; }
        /* ── tables ── */
        table { width:100%; border-collapse:collapse; font-size:.86rem; }
        th,td { padding:5px 7px; border-bottom:1px solid var(--line); text-align:left; }
        th { color:var(--muted); font-weight:600; font-size:.78rem; text-transform:uppercase; letter-spacing:.05em; }
        tbody tr:last-child td { border-bottom:none; }
        .footnote { font-size:.72rem; color:#777; margin-top:4px; }
        /* ── time range buttons ── */
        .range-btns { display:flex; gap:6px; margin-bottom:8px; }
        .range-btn { padding:4px 12px; border:1px solid var(--line); background:var(--card); border-radius:6px; cursor:pointer; font-size:.82rem; }
        .range-btn.active { background:var(--tab-active); color:#fff; border-color:var(--tab-active); }
        /* ── responsive ── */
        @media(max-width:900px) {
            .row2,.row2-eq,.row3 { grid-template-columns:1fr; }
        }
    </style>
</head>
<body>
<div class=\"wrap\">
    <!-- Header -->
    <div class=\"top\">
        <h1>&#9889; Energy Assistant</h1>
        <div class=\"stamp\" id=\"stamp\">loading&hellip;</div>
    </div>

    <!-- KPI bar (always visible) -->
    <div class=\"kpis\" id=\"kpiBar\">
        <div class=\"card\"><div class=\"k\">Grid</div><div class=\"v\" id=\"kGrid\">-</div></div>
        <div class=\"card\"><div class=\"k\">PV</div><div class=\"v\" id=\"kPv\">-</div></div>
        <div class=\"card\"><div class=\"k\">Import Price</div><div class=\"v\" id=\"kPrice\">-</div></div>
        <div class=\"card\"><div class=\"k\">Export Price</div><div class=\"v\" id=\"kExport\">-</div></div>
        <div class=\"card\"><div class=\"k\">Dry Run</div><div class=\"v\" id=\"kDryRun\">-</div></div>
        <div id=\"batteryKpis\"></div>
    </div>

    <!-- Tab navigation -->
    <div class=\"tab-nav\">
        <button class=\"tab-btn active\" data-tab=\"live\">Live</button>
        <button class=\"tab-btn\" data-tab=\"plan\">Plan</button>
        <button class=\"tab-btn\" data-tab=\"history\">History</button>
    </div>

    <!-- ══ TAB: Live ══════════════════════════════════════════════════════════ -->
    <div id=\"tab-live\" class=\"tab-pane active\">
        <div class=\"row2\">
            <div class=\"panel\">
                <h2>Devices</h2>
                <table id=\"devicesTable\">
                    <thead><tr><th>Device</th><th>Role</th><th>Power W</th><th>SoC %</th><th>OK</th></tr></thead>
                    <tbody></tbody>
                </table>
                <p class=\"footnote\">&#185; no live meter &mdash; planned value from active forecast</p>
            </div>
            <div class=\"panel\">
                <h2>Active Setpoints</h2>
                <table id=\"setpointsTable\">
                    <thead><tr><th>Device</th><th>Mode</th><th>Policy</th><th>W</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>
        <div class=\"full\">
            <div class=\"panel\">
                <h2>Battery Ledger</h2>
                <table id=\"ledgerTable\">
                    <thead><tr><th>Device</th><th>Stored kWh</th><th>SoC %</th><th>Capacity kWh</th><th>Basis &euro;/kWh</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- ══ TAB: Plan ══════════════════════════════════════════════════════════ -->
    <div id=\"tab-plan\" class=\"tab-pane\">
        <div id=\"planMeta\" class=\"footnote\" style=\"margin-bottom:8px\"></div>
        <div class=\"full panel\">
            <h2>Energy Flow &mdash; Supply &amp; Demand</h2>
            <div id=\"chartFlow\" style=\"height:280px\"></div>
        </div>
        <div class=\"row2-eq\">
            <div class=\"panel\">
                <h2>PV &amp; Consumption Forecast</h2>
                <div id=\"chartForecast\" style=\"height:220px\"></div>
            </div>
            <div class=\"panel\">
                <h2>Electricity Prices</h2>
                <div id=\"chartPrices\" style=\"height:220px\"></div>
            </div>
        </div>
        <div class=\"row2-eq\">
            <div class=\"panel\">
                <h2>Per-step Saving vs Baseline</h2>
                <div id=\"chartSaving\" style=\"height:200px\"></div>
            </div>
            <div class=\"panel\">
                <h2>Cumulative Saving</h2>
                <div id=\"chartCumSaving\" style=\"height:200px\"></div>
            </div>
        </div>
        <div class=\"full panel\">
            <h2>Battery SoC Trajectory</h2>
            <div id=\"chartSoc\" style=\"height:220px\"></div>
        </div>
    </div>

    <!-- ══ TAB: History ═══════════════════════════════════════════════════════ -->
    <div id=\"tab-history\" class=\"tab-pane\">
        <div class=\"range-btns\">
            <button class=\"range-btn active\" data-h=\"6\">6 h</button>
            <button class=\"range-btn\" data-h=\"12\">12 h</button>
            <button class=\"range-btn\" data-h=\"24\">24 h</button>
            <button class=\"range-btn\" data-h=\"48\">48 h</button>
            <button class=\"range-btn\" data-h=\"168\">7 d</button>
        </div>
        <div class=\"row2-eq\">
            <div class=\"panel\">
                <h2>Battery SoC History</h2>
                <div id=\"chartHistSoc\" style=\"height:300px\"></div>
            </div>
            <div class=\"panel\">
                <h2>Power History (Grid / PV / Household)</h2>
                <div id=\"chartHistPower\" style=\"height:300px\"></div>
            </div>
        </div>
        <div class=\"row2-eq\">
            <div class=\"panel\">
                <h2>Battery Power History</h2>
                <div id=\"chartHistBatPower\" style=\"height:280px\"></div>
            </div>
            <div class=\"panel\">
                <h2>Ledger: Cost Basis History</h2>
                <p id=\"histBasisNote\" class=\"footnote\" style=\"padding:12px\">Basis is persisted as a single snapshot (no time-series). Shown here as the most-recent value per battery from /api/ledger.</p>
                <div id=\"chartHistBasis\" style=\"height:240px\"></div>
            </div>
        </div>
    </div>
</div>

<script>
// ── utilities ─────────────────────────────────────────────────────────────────
function fmt(n, d=2) {
    if (n === null || n === undefined || !Number.isFinite(Number(n))) return '-';
    return Number(n).toFixed(d);
}
function setText(id, txt, cls='') {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = txt;
    el.className = 'v ' + cls;
}
function tableRows(id, rows) {
    const tbody = document.querySelector('#' + id + ' tbody');
    if (!tbody) return;
    tbody.innerHTML = rows.map(r =>
        '<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>'
    ).join('');
}
const PLT = {margin:{l:46,r:12,t:8,b:40}, paper_bgcolor:'white', plot_bgcolor:'white',
             legend:{orientation:'h', y:-0.18}, font:{size:11}};
const PLT_OPT = {responsive:true, displayModeBar:false};

function mkLayout(extra) { return Object.assign({}, PLT, extra); }

// Convert a UTC ISO string to a local-timezone naive ISO string so Plotly
// displays in the browser's local time (Plotly treats timezone-free strings
// as local time; strings with +00:00 are displayed as UTC).
function utcToLocal(iso) {
    const d = new Date(iso);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}` +
           `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function localTs(arr) { return (arr || []).map(utcToLocal); }

// ── tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        if (btn.dataset.tab === 'plan')    refreshPlan();
        if (btn.dataset.tab === 'history') refreshHistory(currentHours);
    });
});

// ── KPI helpers ───────────────────────────────────────────────────────────────
function activePolicies(plan, nowIso) {
    const now = new Date(nowIso).getTime();
    const map = {};
    for (const i of (plan.intents || [])) {
        const ts = new Date(i.timestep).getTime();
        if (ts <= now) {
            const cur = map[i.device_id];
            if (!cur || ts > cur.ts)
                map[i.device_id] = {ts, cp: i.charge_policy || 'auto', dp: i.discharge_policy || 'meet_load_only'};
        }
    }
    return map;
}

// ── LIVE tab ──────────────────────────────────────────────────────────────────
async function refreshLive() {
    const [sR, pR, cR] = await Promise.all([
        fetch('api/status'), fetch('api/plan'), fetch('api/config'),
    ]);
    const status = await sR.json();
    const plan   = await pR.json();
    const cfg    = await cR.json();

    // Build role lookup
    const roleMap = {};
    for (const d of (cfg.devices || [])) roleMap[d.device_id] = d.role;

    // PV device = first producer in status
    const pvDev = (status.devices || []).find(d => roleMap[d.device_id] === 'producer');

    const gp = Number(status.grid_power_w || 0);
    setText('kGrid', Math.round(gp) + ' W', gp > 50 ? 'warn' : (gp < -50 ? 'ok' : ''));
    setText('kPv', pvDev ? (Math.round(-Number(pvDev.power_w || 0)) + ' W') : '-', pvDev && pvDev.power_w < -50 ? 'ok' : '');
    setText('kPrice', fmt(status.current_price_eur_per_kwh, 4) + ' €/kWh');
    setText('kExport', fmt(status.pv_opportunity_price_eur_per_kwh, 4) + ' €/kWh');
    setText('kDryRun', status.dry_run ? 'YES' : 'NO', status.dry_run ? 'warn' : 'ok');
    document.getElementById('stamp').textContent = 'updated ' + new Date(status.timestamp).toLocaleString();

    // Battery KPI cards (dynamic — inject into #batteryKpis)
    const batteryDevs = (status.devices || []).filter(d => d.soc_pct != null);
    document.getElementById('batteryKpis').innerHTML = batteryDevs.map(d => {
        const pct = Number(d.soc_pct);
        const cls = pct < 15 ? 'bad' : (pct > 80 ? 'ok' : '');
        return `<div class=\"card\"><div class=\"k\">${d.device_id}</div>
                 <div class=\"v ${cls}\">${fmt(pct, 0)} %</div></div>`;
    }).join('');

    const policies = activePolicies(plan, status.timestamp);

    // Devices table
    tableRows('devicesTable', (status.devices || []).map(d => {
        const liveW = Number(d.power_w);
        const fw    = Number(d.forecast_power_w);
        const use   = Boolean(d.is_virtual) && Number.isFinite(fw);
        const pw    = use ? Math.round(fw) + ' &#185;' : String(Math.round(liveW || 0));
        const role  = roleMap[d.device_id] || '-';
        return [d.device_id, role, pw,
                d.soc_pct == null ? '-' : fmt(d.soc_pct, 1),
                d.available ? 'yes' : 'no'];
    }));

    // Setpoints table
    tableRows('setpointsTable', (status.setpoints || []).map(sp => {
        const p = policies[sp.device_id] || {cp:'auto', dp:'meet_load_only'};
        return [sp.device_id, sp.mode || '-',
                `c=${p.cp}<br>d=${p.dp}`,
                String(Math.round(Number(sp.setpoint_w || 0)))];
    }));

    // Ledger table
    tableRows('ledgerTable', (status.ledger || []).map(l => {
        const stored = Number(l.stored_energy_kwh || 0);
        const cap    = Number(l.capacity_kwh || 1);
        const soc    = stored / cap * 100;
        return [l.device_id, fmt(stored, 2), fmt(soc, 1) + ' %', fmt(cap, 2),
                fmt(l.cost_basis_eur_per_kwh, 4)];
    }));
}

// ── PLAN tab ──────────────────────────────────────────────────────────────────
async function refreshPlan() {
    const [pR, fR, sR] = await Promise.all([
        fetch('api/plan'), fetch('api/forecast'), fetch('api/status'),
    ]);
    const plan     = await pR.json();
    const fc       = await fR.json();
    const status   = await sR.json();

    const intents  = plan.intents || [];
    const ts       = fc.timestamps || [];
    const prices   = fc.prices || [];
    const epPrices = fc.export_prices || [];
    const pvKw     = fc.pv_kw || [];
    const consKw   = fc.consumption_kw || [];
    const stepH    = (Number(fc.step_minutes) || 60) / 60;
    const stCap    = fc.storage_capacity || {};

    if (!ts.length) {
        document.getElementById('planMeta').textContent = 'No plan available yet.';
        return;
    }

    const created = plan.created_at ? new Date(plan.created_at).toLocaleString() : '-';
    document.getElementById('planMeta').textContent =
        `Plan created: ${created}  ·  step: ${fc.step_minutes} min  ·  ${ts.length} steps`;

    // Build per-timestamp lookup from plan intents
    const tsMs = ts.map(t => new Date(t).getTime());
    function nearestIdx(iso) {
        const ms = new Date(iso).getTime();
        let bi = 0, bd = Infinity;
        for (let i = 0; i < tsMs.length; i++) { const d = Math.abs(tsMs[i]-ms); if (d<bd){bd=d;bi=i;} }
        return bi;
    }

    const chargeByDev   = {};
    const dischargeByDev = {};
    const storageDevs   = [...new Set(intents.map(i => i.device_id))];

    for (const dev of storageDevs) {
        chargeByDev[dev]    = new Array(ts.length).fill(0);
        dischargeByDev[dev] = new Array(ts.length).fill(0);
    }
    for (const i of intents) {
        const idx = nearestIdx(i.timestep);
        const kw = Number(i.planned_kw || 0);
        if (kw > 0) chargeByDev[i.device_id][idx]    += kw;
        else        dischargeByDev[i.device_id][idx]  += -kw;
    }

    const totalChargeKw    = ts.map((_,i) => storageDevs.reduce((s,d) => s + chargeByDev[d][i],    0));
    const totalDischargeKw = ts.map((_,i) => storageDevs.reduce((s,d) => s + dischargeByDev[d][i], 0));

    const gridImportKw = ts.map((_,i) => Math.max(0, consKw[i] + totalChargeKw[i] - pvKw[i] - totalDischargeKw[i]));
    const gridExportKw = ts.map((_,i) => Math.max(0, pvKw[i] + totalDischargeKw[i] - consKw[i] - totalChargeKw[i]));

    // Cost metrics
    const baselineCost = ts.map((_,i) => {
        const net = consKw[i] - pvKw[i];
        return net > 0 ? net * stepH * prices[i]
                       : net * stepH * epPrices[i];  // net<0 → exporting pv
    });
    const optCost = ts.map((_,i) =>
        prices[i] * gridImportKw[i] * stepH - epPrices[i] * gridExportKw[i] * stepH
    );
    const saving = ts.map((_,i) => baselineCost[i] - optCost[i]);
    const cumSaving = [];
    let run = 0;
    for (const v of saving) { run += v; cumSaving.push(run); }

    const totalSave = cumSaving[cumSaving.length - 1] || 0;
    document.getElementById('planMeta').textContent +=
        `  ·  est. saving: ${totalSave >= 0 ? '+' : ''}${fmt(totalSave, 3)} €`;

    // Convert UTC plan timestamps to local time once (Plotly displays tz-naive strings as local)
    const tsLocal = localTs(ts);

    // ── Panel 1: Energy flow stacked bars ─────────────────────────────────────
    const flowTraces = [
        {name:'PV',       type:'bar', x:tsLocal, y:pvKw,           marker:{color:'#f0c040'}, hovertemplate:'%{y:.2f} kW'},
        {name:'Discharge',type:'bar', x:tsLocal, y:totalDischargeKw,marker:{color:'#4caf7d'}, hovertemplate:'%{y:.2f} kW'},
        {name:'Grid imp', type:'bar', x:tsLocal, y:gridImportKw,    marker:{color:'#e07070'}, hovertemplate:'%{y:.2f} kW'},
        {name:'Consumption',type:'bar',x:tsLocal,y:consKw.map(v=>-v),marker:{color:'#6b7bb5'}, hovertemplate:'%{y:.2f} kW'},
        {name:'Charge',   type:'bar', x:tsLocal, y:totalChargeKw.map(v=>-v),marker:{color:'#3a9ad9'}, hovertemplate:'%{y:.2f} kW'},
        {name:'Grid exp', type:'bar', x:tsLocal, y:gridExportKw.map(v=>-v), marker:{color:'#b07030'}, hovertemplate:'%{y:.2f} kW'},
    ];
    Plotly.newPlot('chartFlow', flowTraces,
        mkLayout({barmode:'relative', yaxis:{title:'kW', zeroline:true}, xaxis:{}}),
        PLT_OPT);

    // ── Panel 2: Forecast ─────────────────────────────────────────────────────
    const fcTraces = [
        {name:'PV forecast',   mode:'lines', x:tsLocal, y:pvKw,   line:{color:'#f0c040'}, hovertemplate:'%{y:.2f} kW'},
        {name:'Consumption fc',mode:'lines', x:tsLocal, y:consKw, line:{color:'#6b7bb5'}, hovertemplate:'%{y:.2f} kW'},
    ];
    Plotly.newPlot('chartForecast', fcTraces,
        mkLayout({yaxis:{title:'kW'}, xaxis:{}}), PLT_OPT);

    // ── Panel 3: Prices ───────────────────────────────────────────────────────
    const priceTraces = [
        {name:'Import price', mode:'lines', x:tsLocal, y:prices,   line:{color:'#e07070', dash:'solid'}},
        {name:'Export price', mode:'lines', x:tsLocal, y:epPrices, line:{color:'#4caf7d', dash:'dot'}},
    ];
    Plotly.newPlot('chartPrices', priceTraces,
        mkLayout({yaxis:{title:'€/kWh'}, xaxis:{}}), PLT_OPT);

    // ── Panel 4: Per-step saving ──────────────────────────────────────────────
    const savingTraces = [{
        name:'Saving', type:'bar', x:tsLocal, y:saving,
        marker:{color: saving.map(v => v >= 0 ? '#4caf7d' : '#e07070')},
        hovertemplate:'%{y:.4f} €',
    }];
    Plotly.newPlot('chartSaving', savingTraces,
        mkLayout({yaxis:{title:'€'}, xaxis:{}}), PLT_OPT);

    // ── Panel 5: Cumulative saving ────────────────────────────────────────────
    Plotly.newPlot('chartCumSaving',
        [{name:'Cumulative', mode:'lines', fill:'tozeroy', x:tsLocal, y:cumSaving,
          line:{color:'#4caf7d'}, fillcolor:'rgba(76,175,125,0.15)', hovertemplate:'%{y:.4f} €'}],
        mkLayout({yaxis:{title:'€'}, xaxis:{}}), PLT_OPT);

    // ── Panel 6: SoC trajectories ─────────────────────────────────────────────
    const deviceSocPct = {};
    for (const d of (status.devices || []))
        if (d.soc_pct != null) deviceSocPct[d.device_id] = Number(d.soc_pct);

    const socTraces = [];
    for (const dev of storageDevs) {
        if (deviceSocPct[dev] == null) continue;
        const cap   = (stCap[dev] || {}).capacity_kwh   || 7.5;
        const etaC  = (stCap[dev] || {}).charge_efficiency    || 0.95;
        const etaD  = (stCap[dev] || {}).discharge_efficiency || 0.95;
        const minS  = (stCap[dev] || {}).min_soc_pct || 0;
        const maxS  = (stCap[dev] || {}).max_soc_pct || 100;
        let energy  = cap * deviceSocPct[dev] / 100;
        const socPct = [deviceSocPct[dev]];
        const socTs  = [utcToLocal(new Date(new Date(ts[0]).getTime() - (Number(fc.step_minutes)||60)*60000).toISOString())];
        for (let i = 0; i < ts.length; i++) {
            energy += etaC * chargeByDev[dev][i] * stepH;
            energy -= dischargeByDev[dev][i] / etaD * stepH;
            energy  = Math.max(cap*minS/100, Math.min(cap*maxS/100, energy));
            socPct.push(energy / cap * 100);
            socTs.push(tsLocal[i]);
        }
        socTraces.push({name:dev, mode:'lines', x:socTs, y:socPct,
            hovertemplate:'%{y:.1f} %'});
    }
    Plotly.newPlot('chartSoc', socTraces,
        mkLayout({yaxis:{title:'SoC %', range:[0,105]}, xaxis:{}}), PLT_OPT);
}

// ── HISTORY tab ───────────────────────────────────────────────────────────────
let currentHours = 6;

document.querySelectorAll('.range-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentHours = Number(btn.dataset.h);
        refreshHistory(currentHours);
    });
});

async function refreshHistory(hours) {
    let resp;
    try {
        const r = await fetch(`api/history?hours=${hours}`);
        resp = await r.json();
    } catch(e) { console.error('history fetch failed', e); return; }

    // New response shape: {measurements: {...}, ledger: {...}}
    const histData   = resp.measurements || resp;   // backwards-compat if old shape
    const ledgerHist = resp.ledger || {};

    // Also fetch current ledger snapshot for devices with no history yet
    let ledgerSnap = [];
    try { ledgerSnap = await (await fetch('api/ledger')).json(); } catch(_) {}

    // ── SoC history ───────────────────────────────────────────────────────────
    const socTraces = [];
    for (const [did, rows] of Object.entries(histData)) {
        const hasSoc = rows.some(r => r.soc_pct != null);
        if (!hasSoc) continue;
        const x = localTs(rows.map(r => r.t));
        const y = rows.map(r => r.soc_pct);
        socTraces.push({name: did, mode:'lines', x, y, hovertemplate:'%{y:.1f} %'});
    }
    if (socTraces.length) {
        Plotly.newPlot('chartHistSoc', socTraces,
            mkLayout({yaxis:{title:'SoC %', range:[0,105]}, xaxis:{}}), PLT_OPT);
    }

    // ── Power history (meters + PV) ───────────────────────────────────────────
    const pwrTraces = [];
    const pwrDids   = Object.keys(histData).filter(did => !histData[did].some(r => r.soc_pct != null && r.power_w == null));
    for (const did of pwrDids) {
        const rows = histData[did];
        if (!rows.length) continue;
        const hasPwr = rows.some(r => r.power_w != null);
        if (!hasPwr) continue;
        const x = localTs(rows.filter(r => r.power_w != null).map(r => r.t));
        const y = rows.filter(r => r.power_w != null).map(r => r.power_w);
        pwrTraces.push({name: did, mode:'lines', x, y, hovertemplate:'%{y:.0f} W'});
    }
    if (pwrTraces.length) {
        Plotly.newPlot('chartHistPower', pwrTraces,
            mkLayout({yaxis:{title:'W'}, xaxis:{}}), PLT_OPT);
    }

    // ── Battery power history ─────────────────────────────────────────────────
    const batPwrTraces = [];
    for (const [did, rows] of Object.entries(histData)) {
        const hasSoc = rows.some(r => r.soc_pct != null);
        if (!hasSoc) continue;
        const x = localTs(rows.filter(r => r.power_w != null).map(r => r.t));
        const y = rows.filter(r => r.power_w != null).map(r => r.power_w);
        if (x.length) batPwrTraces.push({name: did, mode:'lines', x, y, hovertemplate:'%{y:.0f} W'});
    }
    if (batPwrTraces.length) {
        Plotly.newPlot('chartHistBatPower', batPwrTraces,
            mkLayout({yaxis:{title:'W', zeroline:true}, xaxis:{}}), PLT_OPT);
    }

    // ── Cost Basis history (time-series from ledger_history table) ─────────────
    const basisTraces = [];
    for (const [did, rows] of Object.entries(ledgerHist)) {
        if (!rows.length) continue;
        basisTraces.push({
            name: did + ' basis', mode:'lines',
            x: localTs(rows.map(r => r.t)),
            y: rows.map(r => r.cost_basis_eur_per_kwh),
            hovertemplate: '%{y:.4f} €/kWh',
        });
    }
    // If no history yet, fall back to current snapshot as a reference bar
    if (!basisTraces.length && ledgerSnap.length) {
        const basisNote = document.getElementById('histBasisNote');
        if (basisNote) basisNote.style.display = '';
        Plotly.newPlot('chartHistBasis',
            [{type:'bar',
              x: ledgerSnap.map(l => l.device_id),
              y: ledgerSnap.map(l => l.cost_basis_eur_per_kwh || 0),
              text: ledgerSnap.map(l => fmt(l.cost_basis_eur_per_kwh, 4) + ' €/kWh'),
              textposition:'auto', marker:{color:'#3a9ad9'}}],
            mkLayout({yaxis:{title:'€/kWh'}, xaxis:{}, margin:{l:46,r:12,t:8,b:60}}), PLT_OPT);
    } else if (basisTraces.length) {
        const basisNote = document.getElementById('histBasisNote');
        if (basisNote) basisNote.style.display = 'none';
        Plotly.newPlot('chartHistBasis', basisTraces,
            mkLayout({yaxis:{title:'€/kWh'}, xaxis:{}}), PLT_OPT);
    }
}

// ── Auto-refresh logic ────────────────────────────────────────────────────────
function activeTab() {
    const btn = document.querySelector('.tab-btn.active');
    return btn ? btn.dataset.tab : 'live';
}

async function refreshAll() {
    const tab = activeTab();
    if (tab === 'live')    await refreshLive();
    if (tab === 'plan')    await refreshPlan();
    if (tab === 'history') await refreshHistory(currentHours);
}

// Initial load
refreshLive().catch(console.error);

// Periodic refresh
setInterval(() => {
    const tab = activeTab();
    if (tab === 'live') refreshLive().catch(console.error);
}, 10000);
setInterval(() => {
    const tab = activeTab();
    if (tab === 'plan') refreshPlan().catch(console.error);
}, 60000);
setInterval(() => {
    const tab = activeTab();
    if (tab === 'history') refreshHistory(currentHours).catch(console.error);
}, 300000);
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

        # Inject tariff prices if no ForecastProvider produces PRICE data.
        # The MILP fetches prices from tariffs internally, but the cached forecasts
        # dict is used by /api/forecast for the UI — it needs prices too.
        if not forecasts.get(ForecastQuantity.PRICE):
            for tariff in self._tariffs.values():
                try:
                    sched = await tariff.price_schedule(self._horizon)
                    if sched and any(tp.price_eur_per_kwh > 0.001 for tp in sched):
                        forecasts[ForecastQuantity.PRICE] = [
                            ForecastPoint(timestamp=tp.timestamp, value=tp.price_eur_per_kwh)
                            for tp in sched
                        ]
                        break
                except Exception:  # noqa: BLE001
                    pass

        self._last_forecast_pts = forecasts  # cache for /api/forecast

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

            live = LiveSituation(
                timestamp=now,
                grid_power_w=grid_power_w,
                dt_hours=0.0,
                device_states=device_states_map,
                current_price_eur_per_kwh=current_price,
                pv_opportunity_price_eur_per_kwh=self._pv_opportunity_price,
            )

            devices_payload = []
            for s in device_states_map.values():
                cfg = self._cfg.devices.get(s.device_id, {})
                devices_payload.append(
                    {
                        "device_id": s.device_id,
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
            pts_pv    = sorted(self._last_forecast_pts.get(ForecastQuantity.PV_GENERATION, []),
                               key=lambda p: p.timestamp)
            pts_cons_raw = self._last_forecast_pts.get(ForecastQuantity.CONSUMPTION, [])

            def nn_value(pts: list[ForecastPoint], ts: datetime) -> float:
                if not pts:
                    return 0.0
                best = min(pts, key=lambda p: abs((p.timestamp - ts).total_seconds()))
                return float(best.value)

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
                    from ..core.models import TariffPoint
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
                "export_prices": [ep_value(t) for t in timestamps],
                "pv_kw":         [nn_value(pts_pv,    t) for t in timestamps],
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

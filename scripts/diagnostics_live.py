"""Show live status and the active plan from the running energy-assistant server.

Connects to the server REST API and renders the same 5-panel Plotly diagnostics
as ``diagnostics_sim.py``, but driven by the *active live plan*:

  * Current grid power, spot price, PV export price, dry-run flag
  * Per-device power / SoC table
  * Current setpoints (what the control loop is sending right now)
  * Battery ledger (stored energy + cost basis from SQLite)
    * Interactive Plotly chart: energy flows, forecasts, per-step saving, cumulative saving, SoC

Prerequisite — start the server first::

    python -m energy_assistant

Usage::

    python scripts/diagnostics_live.py
    python scripts/diagnostics_live.py --url http://192.168.1.10:8088
    python scripts/diagnostics_live.py --config /path/to/config.yaml
    python scripts/diagnostics_live.py --save-html plan.html
    python scripts/diagnostics_live.py --no-chart
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

# Insert src/ into the path so the script runs without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from energy_assistant.config.yaml import YamlConfigLoader
from energy_assistant.core.models import DeviceRole, DeviceState
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.loader.device_loader import build as build_from_config
from energy_assistant.plugins._iobroker.pool import IoBrokerConnectionPool
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer

import visualize_optimizer as _vo


# ── Core async routine ────────────────────────────────────────────────────────

async def _fetch_and_display(
    api_base_url: str,
    save_html: str | None,
    no_chart: bool,
    config_path: Path,
    price_tariff_id: str | None,
) -> None:
    active_policies: dict[str, tuple[str, str]] = {}

    # ── Live Status ───────────────────────────────────────────────────────────
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{api_base_url}/api/status", timeout=5)
        resp.raise_for_status()
        s = resp.json()

        # Also fetch the active plan so we can show policy semantics next to
        # the currently effective setpoints.
        rp = await client.get(f"{api_base_url}/api/plan", timeout=5)
        rp.raise_for_status()
        plan_data = rp.json()

    if plan_data.get("intents"):
        now = pd.Timestamp(s["timestamp"])
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        else:
            now = now.tz_convert("UTC")

        for intent in plan_data["intents"]:
            ts = pd.Timestamp(intent["timestep"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            if ts <= now:
                did = str(intent.get("device_id"))
                cur = active_policies.get(did)
                if cur is None or ts > pd.Timestamp(cur[2]):
                    cp = str(intent.get("charge_policy") or "auto")
                    dp = str(intent.get("discharge_policy") or "meet_load_only")
                    active_policies[did] = (cp, dp, ts.isoformat())

    # drop helper timestamp from map values
    active_policies = {
        did: (vals[0], vals[1])
        for did, vals in active_policies.items()
    }

    print(f"  Timestamp  : {s['timestamp']}")
    grid_w = s["grid_power_w"]
    print(f"  Grid       : {grid_w:+.0f} W  ({'importing' if grid_w > 0 else 'exporting'})")
    print(f"  Price      : {s['current_price_eur_per_kwh']:.4f} €/kWh")
    print(f"  Export pr. : {s['pv_opportunity_price_eur_per_kwh']:.4f} €/kWh")
    print(f"  Dry run    : {s['dry_run']}")
    print()

    # Device states table
    if s["devices"]:
        df_dev = pd.DataFrame(s["devices"])
        cols   = [c for c in ("device_id", "power_w", "soc_pct", "available", "timestamp")
                  if c in df_dev.columns]
        df_dev = df_dev[cols]
        df_dev.columns = [{"device_id": "Device", "power_w": "Power W",
                            "soc_pct": "SoC %", "available": "OK",
                            "timestamp": "Updated"}.get(c, c) for c in cols]
        print(df_dev.to_string(index=False))
        print()

    # Current setpoints
    print("  Current setpoints (what would be sent right now):")
    for sp in s["setpoints"]:
        w     = sp["setpoint_w"] or 0
        arrow = (f"charge    {w:+.0f} W" if w > 0 else
                 f"discharge {w:+.0f} W" if w < 0 else "hold (0 W)")
        cp, dp = active_policies.get(sp["device_id"], ("auto", "meet_load_only"))
        print(
            f"    {sp['device_id']:<22}  mode={sp['mode']:<10}  "
            f"policy(c={cp}, d={dp})  → {arrow}"
        )
    print()

    # Battery ledger
    print("  Battery ledger (stored energy + cost basis from SQLite):")
    for e in s["ledger"]:
        stored = e["stored_energy_kwh"] or 0
        cap    = e["capacity_kwh"] or 1
        basis  = e["cost_basis_eur_per_kwh"] or 0
        soc    = stored / cap * 100
        print(f"    {e['device_id']:<22}  "
              f"stored={stored:.2f}/{cap:.1f} kWh ({soc:.0f}%)  "
              f"basis={basis:.4f} €/kWh")

    if no_chart:
        return

    # ── Live Plan Chart (Plotly) ──────────────────────────────────────────────
    async with httpx.AsyncClient() as client:
        rp = await client.get(f"{api_base_url}/api/plan",   timeout=5)
        rl = await client.get(f"{api_base_url}/api/ledger", timeout=5)
        rp.raise_for_status()
        rl.raise_for_status()
        plan_data = rp.json()
        ledger    = {e["device_id"]: e for e in rl.json()}

    if not plan_data["intents"]:
        print("\n  No plan available yet — wait for the planning loop to run "
              "(plan_interval_s in config.yaml).")
        return

    step_min = int(plan_data["step_minutes"])
    step_h   = step_min / 60.0

    df = pd.DataFrame(plan_data["intents"])
    df["timestep"]   = pd.to_datetime(df["timestep"])
    df["planned_kw"] = df["planned_kw"].fillna(0.0)
    if df["timestep"].dt.tz is None:
        df["timestep"] = df["timestep"].dt.tz_localize("UTC")
    else:
        df["timestep"] = df["timestep"].dt.tz_convert("UTC")
    devices = list(df["device_id"].unique())
    live_soc_pct = {
        str(d.get("device_id")): float(d.get("soc_pct"))
        for d in s.get("devices", [])
        if d.get("device_id") is not None and d.get("soc_pct") is not None
    }
    timestamps = sorted({ts.to_pydatetime() for ts in df["timestep"]})
    if not timestamps:
        print("\n  No timestamps in active plan.")
        return
    N = len(timestamps)
    T = range(N)
    t_index = {ts: i for i, ts in enumerate(timestamps)}

    # Build storage list from config so we can include wear costs in saving metrics.
    app_config = YamlConfigLoader(config_path).load()
    device_registry, tariffs, _ = build_from_config(app_config)

    iobroker_pool = None
    if app_config.backends.iobroker:
        iobroker_pool = IoBrokerConnectionPool()
    ctx = BuildContext(backends=app_config.backends, iobroker_pool=iobroker_pool, ha_client=None)
    forecast_providers = _vo._build_forecast_providers(app_config, ctx)

    storage_constraints = {
        sc.device_id: sc
        for dev in device_registry.by_role(DeviceRole.STORAGE)
        if (sc := getattr(dev, "storage_constraints", None)) is not None
    }

    # Resolve forecast layers aligned to plan timestamps.
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    horizon = max(timestamps[-1] - now + timedelta(minutes=step_min), timedelta(minutes=step_min))
    raw_prices, _ = await _vo._fetch_raw_prices(tariffs, price_tariff_id, horizon)
    raw_pv, _ = await _vo._fetch_raw_pv(forecast_providers, horizon)
    raw_consumption, _ = await _vo._fetch_raw_consumption(forecast_providers, timestamps, horizon)

    prices = _vo._align(raw_prices, timestamps)
    pv_kw = _vo._align(raw_pv, timestamps)
    consumption_fc = _vo._align(raw_consumption, timestamps)
    baseline_kw = float(app_config.optimizer.get("baseline_load_kw", 0.0))
    consumption_kw = [max(baseline_kw, v) for v in consumption_fc]

    # Per-device charge/discharge from the active plan intents.
    charge_kw: dict[str, list[float]] = {dev: [0.0] * N for dev in devices}
    discharge_kw: dict[str, list[float]] = {dev: [0.0] * N for dev in devices}
    for _, row in df.iterrows():
        dev = str(row["device_id"])
        idx = t_index[row["timestep"].to_pydatetime()]
        kw = float(row["planned_kw"] or 0.0)
        if kw >= 0.0:
            charge_kw[dev][idx] = kw
        else:
            discharge_kw[dev][idx] = -kw

    total_charge_kw = [sum(charge_kw[d][t] for d in devices) for t in T]
    total_discharge_kw = [sum(discharge_kw[d][t] for d in devices) for t in T]
    net_grid_kw = [
        consumption_kw[t] + total_charge_kw[t] - pv_kw[t] - total_discharge_kw[t]
        for t in T
    ]
    grid_import_kw = [max(0.0, v) for v in net_grid_kw]
    grid_export_kw = [max(0.0, -v) for v in net_grid_kw]

    # Resolve export prices for the same timestamp grid.
    optimizer = MilpHigsOptimizer(step_minutes=step_min)
    ledger_basis = {
        did: float((entry.get("cost_basis_eur_per_kwh") or 0.0))
        for did, entry in ledger.items()
    }
    device_states = {
        did: DeviceState(device_id=did, soc_pct=(entry.get("stored_energy_kwh") or 0.0)
                         / max(1e-9, (entry.get("capacity_kwh") or 1.0)) * 100.0)
        for did, entry in ledger.items()
    }
    export_prices = await optimizer._resolve_export_prices(
        OptimizationContext(
            device_states=device_states,
            storage_constraints=list(storage_constraints.values()),
            tariffs=tariffs,
            forecasts={},
            horizon=horizon,
            battery_cost_basis=ledger_basis,
        ),
        timestamps,
    )

    # Cost metrics using the same definitions as simulation diagnostics.
    opt_cost = [
        prices[t] * grid_import_kw[t] * step_h
        - export_prices[t] * grid_export_kw[t] * step_h
        + sum(
            storage_constraints[d].degradation_cost_per_kwh
            * storage_constraints[d].charge_efficiency
            * charge_kw[d][t]
            * step_h
            for d in devices
            if d in storage_constraints
        )
        for t in T
    ]
    baseline_cost = [
        max(0.0, consumption_kw[t] - pv_kw[t]) * step_h * prices[t]
        - max(0.0, pv_kw[t] - consumption_kw[t]) * step_h * export_prices[t]
        for t in T
    ]
    saving_per_step = [b - o for b, o in zip(baseline_cost, opt_cost)]
    cum_saving: list[float] = []
    running = 0.0
    for v in saving_per_step:
        running += v
        cum_saving.append(running)

    # SoC trajectories from live ledger and plan intents (N+1 points).
    batteries_plot: dict[str, dict] = {}
    for dev in devices:
        cap = float((ledger.get(dev) or {}).get("capacity_kwh") or 1.0)
        basis = float((ledger.get(dev) or {}).get("cost_basis_eur_per_kwh") or 0.0)
        sc = storage_constraints.get(dev)
        eta_c = float(sc.charge_efficiency) if sc is not None else 1.0
        eta_d = float(sc.discharge_efficiency) if sc is not None else 1.0
        min_soc_pct = float(sc.min_soc_pct) if sc is not None else 0.0
        max_soc_pct = float(sc.max_soc_pct) if sc is not None else 100.0
        e_min = cap * min_soc_pct / 100.0
        e_max = cap * max_soc_pct / 100.0
        if dev in live_soc_pct:
            energy = cap * live_soc_pct[dev] / 100.0
        else:
            energy = float((ledger.get(dev) or {}).get("stored_energy_kwh") or 0.0)
        energy = max(e_min, min(e_max, energy))
        soc = [energy / cap * 100.0]
        for t in T:
            # Match MILP storage dynamics: e[t] = e[t-1] + eta_c*c - d/eta_d
            charged_kwh = charge_kw[dev][t] * step_h
            discharged_kwh = discharge_kw[dev][t] * step_h
            energy = max(
                e_min,
                min(e_max, energy + eta_c * charged_kwh - discharged_kwh / max(1e-9, eta_d)),
            )
            soc.append(energy / cap * 100.0)
        batteries_plot[dev] = {
            "charge_kw": charge_kw[dev],
            "discharge_kw": discharge_kw[dev],
            "soc_pct": soc,
            "capacity_kwh": cap,
            "cost_basis_eur_per_kwh": basis,
        }

    local_ts = [ts.astimezone() for ts in timestamps]
    created = plan_data["created_at"] or "—"
    fig = _vo._make_flows_figure(
        timestamps=local_ts,
        pv_kw=pv_kw,
        consumption_kw=consumption_kw,
        import_price_eur_per_kwh=prices,
        export_price_eur_per_kwh=export_prices,
        grid_import_kw=grid_import_kw,
        grid_export_kw=grid_export_kw,
        batteries=batteries_plot,
        saving_eur=saving_per_step,
        cumulative_saving_eur=cum_saving,
        step_minutes=step_min,
        title=(
            f"Live Plan: Energy Flows & Cost Impact  ·  created {created}"
            f"  ·  step {step_min} min  ·  {len(plan_data['intents'])} intents"
        ),
    )

    if save_html:
        fig.write_html(save_html)
        print(f"\n  Chart saved → {save_html}")
    else:
        fig.show()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(repo_root / "config.yaml"),
        help="Path to config.yaml  (default: <repo>/config.yaml)",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8088",
        help="Server base URL  (default: http://localhost:8088)",
    )
    parser.add_argument(
        "--price-tariff",
        metavar="ID",
        help="Pin the tariff used for electricity prices",
    )
    parser.add_argument(
        "--save-html",
        metavar="PATH",
        help="Save interactive chart to HTML instead of opening a browser window",
    )
    parser.add_argument(
        "--no-chart",
        action="store_true",
        help="Print live status only; skip the plan chart",
    )
    args = parser.parse_args()
    asyncio.run(
        _fetch_and_display(
            args.url,
            args.save_html,
            args.no_chart,
            Path(args.config),
            args.price_tariff,
        )
    )


if __name__ == "__main__":
    _main()

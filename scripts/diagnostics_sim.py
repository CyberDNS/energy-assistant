"""Simulate the optimizer with live data and show energy-flow diagnostics.

Fetches live prices, PV forecast, consumption profiles and battery SoC from
ioBroker/HA, then runs the MILP optimizer and prints:

  * Data source summary (prices / PV / consumption / SoC)
  * Battery overview table (capacity · cost-basis · terminal-value · discharge threshold)
  * Charge / discharge totals and SoC trajectory per battery
  * Cost comparison: optimised vs no-battery baseline

Five-panel interactive Plotly chart (saved as HTML):
  Panel 1 — Supply (+) / Demand (−) stacked breakdown every time step
    Panel 2 — Forecast panel: PV, consumption, import price, export price
    Panel 3 — Per-step cost saving vs baseline (green = saved, red = extra)
    Panel 4 — Running cumulative saving over the full horizon
    Panel 5 — Battery SoC trajectory over the horizon

Complements ``diagnostics_live.py`` (which shows the server's current plan)
by running the MILP from scratch with live data, using the same Plotly layout
for easy side-by-side comparison.

Usage::

    python scripts/diagnostics_sim.py
    python scripts/diagnostics_sim.py --config /path/to/config.yaml
    python scripts/diagnostics_sim.py --save-html flows.html
    python scripts/diagnostics_sim.py --price-tariff household
    python scripts/diagnostics_sim.py --cost-basis sma_battery=0.12,zendure=0.08
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Insert src/ into the path so the script runs without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pulp

from energy_assistant.config.yaml import YamlConfigLoader
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import (
    DeviceRole,
    DeviceState,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
)
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.loader.device_loader import build as build_from_config
from energy_assistant.plugins._iobroker.pool import IoBrokerConnectionPool
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer

# Import shared helpers from the existing visualize_optimizer script.
import visualize_optimizer as _vo

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)


# ── Core async routine ────────────────────────────────────────────────────────

async def _run(
    config_path: Path,
    price_tariff_id: str | None,
    save_html: str | None,
    cost_basis_override: dict[str, float] | None = None,
) -> None:
    # ── 1. Load config + build devices/tariffs ────────────────────────────────
    app_config = YamlConfigLoader(config_path).load()
    device_registry, tariffs, _ = build_from_config(app_config)

    iobroker_pool = None
    if app_config.backends.iobroker:
        iobroker_pool = IoBrokerConnectionPool()
    ctx = BuildContext(backends=app_config.backends, iobroker_pool=iobroker_pool, ha_client=None)

    # _build_forecast_providers returns build_all_forecasts: PV + all consumption providers
    forecast_providers = _vo._build_forecast_providers(app_config, ctx)

    # ── 2. Fetch data ─────────────────────────────────────────────────────────
    horizon_cap_h = int(app_config.optimizer.get("horizon_hours", 48))
    horizon_cap   = timedelta(hours=horizon_cap_h)
    now           = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    raw_prices, price_src = await _vo._fetch_raw_prices(tariffs, price_tariff_id, horizon_cap)
    raw_pv,     pv_src    = await _vo._fetch_raw_pv(forecast_providers, horizon_cap)

    step_min  = _vo._detect_step_minutes(raw_prices, raw_pv)
    step_td   = timedelta(minutes=step_min)
    step_h    = step_min / 60.0

    horizon   = _vo._infer_horizon(raw_prices, raw_pv, step_td, now, cap=horizon_cap)
    horizon_h = int(horizon.total_seconds() / 3600)
    timestamps = [now + step_td * i for i in range(int(horizon / step_td))]
    N          = len(timestamps)
    T          = range(N)

    prices = _vo._align(raw_prices, timestamps)
    pv_kw  = _vo._align(raw_pv, timestamps)

    raw_consumption, consump_src = await _vo._fetch_raw_consumption(
        forecast_providers, timestamps, horizon
    )
    consumption_fc = _vo._align(raw_consumption, timestamps)

    print(f"  Prices      : {price_src}")
    print(f"  PV forecast : {pv_src}")
    print(f"  Consumption : {consump_src}")
    print(f"  Time step   : {step_min} min  →  {N} steps over {horizon_h} h")

    # ── 3. Live SoC ───────────────────────────────────────────────────────────
    storage_devices     = device_registry.by_role(DeviceRole.STORAGE)
    storage_constraints: list[StorageConstraints] = []
    device_states:       dict[str, DeviceState]   = {}

    for dev in storage_devices:
        sc = getattr(dev, "storage_constraints", None)
        if sc is None:
            continue
        storage_constraints.append(sc)
        raw_soc = await _vo._fetch_soc(dev)
        if raw_soc is None:
            soc = (sc.min_soc_pct + sc.max_soc_pct) / 2
            note = " (fallback)"
        else:
            soc = max(sc.min_soc_pct, min(sc.max_soc_pct, raw_soc))
            note = ""
        device_states[dev.device_id] = DeviceState(
            device_id=dev.device_id,
            soc_pct=soc,
        )
        if raw_soc is not None and abs(raw_soc - soc) > 1e-6:
            print(
                f"  SoC {dev.device_id:20s}: {raw_soc:.1f} %  →  {soc:.1f} % "
                f"(clamped to {sc.min_soc_pct:.1f}–{sc.max_soc_pct:.1f} %){note}"
            )
        else:
            print(f"  SoC {dev.device_id:20s}: {device_states[dev.device_id].soc_pct:.1f} %{note}")

    # ── 4. Cost ledger ────────────────────────────────────────────────────────
    overrides    = cost_basis_override or {}
    current_spot = prices[0] if prices else 0.25
    ledger = BatteryCostLedger()
    for sc in storage_constraints:
        stored = sc.capacity_kwh * (device_states[sc.device_id].soc_pct or 0) / 100
        basis  = overrides.get(sc.device_id, current_spot)
        ledger.initialise(sc.device_id, stored, basis)

    baseline_kw = float(app_config.optimizer.get("baseline_load_kw", 0.0))

    # ── 5. Export prices ──────────────────────────────────────────────────────
    optimizer = MilpHigsOptimizer(step_minutes=step_min)
    export_prices = await optimizer._resolve_export_prices(
        OptimizationContext(
            device_states=device_states,
            storage_constraints=storage_constraints,
            tariffs=tariffs,
            forecasts={},
            horizon=horizon,
            battery_cost_basis=ledger.all_cost_bases(),
        ),
        timestamps,
    )

    # ── 6. Terminal value basis — TV = max(cost_basis, p70 × (η_d − 0.01)) ───
    p70 = sorted(prices)[int(0.70 * len(prices))]
    cost_bases = ledger.all_cost_bases()
    terminal_value_basis = {
        sc.device_id: max(
            cost_bases.get(sc.device_id, current_spot),
            p70 * max(0.0, sc.discharge_efficiency - 0.01),
        )
        for sc in storage_constraints
    }

    # ── 7. Battery overview table ─────────────────────────────────────────────
    export_price_now = export_prices[0] if export_prices else 0.082
    print()
    print(f"  {'Device':<22}  {'Capacity':>9}  {'CostBasis':>9}  "
          f"{'Wear €/kWh':>10}  {'TV €/kWh':>9}  {'DisThres':>9}")
    print("  " + "─" * 83)
    for sc in storage_constraints:
        basis         = cost_bases.get(sc.device_id, current_spot)
        tv            = terminal_value_basis[sc.device_id]
        dis_threshold = tv / sc.discharge_efficiency
        pv_threshold  = export_price_now / sc.charge_efficiency + sc.degradation_cost_per_kwh
        pv_flag = "✓ PV charges" if tv > pv_threshold else "✗ no PV chg"
        print(f"  {sc.device_id:<22}  {sc.capacity_kwh:>7.1f} kWh"
              f"  {basis:>7.4f} €    {sc.degradation_cost_per_kwh:>8.4f} €"
              f"    {tv:>7.4f} €  {dis_threshold:>7.4f} €  {pv_flag}")

    # ── 8. Consumption + net load ─────────────────────────────────────────────
    consumption_kw = [max(baseline_kw, consumption_fc[i]) for i in range(N)]
    net_load       = [(consumption_kw[i] - pv_kw[i]) * step_h for i in range(N)]

    # ── 9. Build and solve MILP ───────────────────────────────────────────────
    context = OptimizationContext(
        device_states=device_states,
        storage_constraints=storage_constraints,
        tariffs=tariffs,
        forecasts={
            ForecastQuantity.PRICE: [
                ForecastPoint(timestamp=ts, value=p) for ts, p in zip(timestamps, prices)
            ],
            ForecastQuantity.PV_GENERATION: [
                ForecastPoint(timestamp=ts, value=v) for ts, v in zip(timestamps, pv_kw)
            ],
            ForecastQuantity.CONSUMPTION: [
                ForecastPoint(timestamp=ts, value=c) for ts, c in zip(timestamps, consumption_kw)
            ],
        },
        horizon=horizon,
        battery_cost_basis=ledger.all_cost_bases(),
    )
    initial_energy = optimizer._initial_energy(storage_constraints, context)
    prob, variables = optimizer._build_model(
        N, step_h, storage_constraints, net_load,
        prices, export_prices, initial_energy, context.battery_cost_basis,
        terminal_value_basis,
    )
    status = prob.solve(optimizer._get_solver())
    print(f"\n  Solver: {pulp.LpStatus[status]}")

    # ── 10. Charge / discharge summary ───────────────────────────────────────
    g_imp_kwh     = [pulp.value(variables["g_imp"][t]) or 0.0 for t in T]
    g_exp_kwh     = [pulp.value(variables["g_exp"][t]) or 0.0 for t in T]
    charge_kwh    = {
        sc.device_id: [pulp.value(variables["c"][(sc.device_id, t)]) or 0.0 for t in T]
        for sc in storage_constraints
    }
    discharge_kwh = {
        sc.device_id: [pulp.value(variables["d"][(sc.device_id, t)]) or 0.0 for t in T]
        for sc in storage_constraints
    }

    print("\n  Battery schedule:")
    for sc in storage_constraints:
        total_c = sum(charge_kwh[sc.device_id])
        total_d = sum(discharge_kwh[sc.device_id])
        e_init  = initial_energy[sc.device_id]
        e_final = pulp.value(variables["e"][(sc.device_id, N - 1)]) or 0
        thr     = terminal_value_basis[sc.device_id] / sc.discharge_efficiency
        print(f"    {sc.device_id:<22}  charged={total_c:.2f} kWh  "
              f"discharged={total_d:.2f} kWh  "
              f"SoC: {e_init:.1f}→{e_final:.1f} kWh  "
              f"(dis_threshold={thr:.4f} €/kWh)")

    # ── 11. Cost analysis ─────────────────────────────────────────────────────
    opt_cost = [
        prices[t] * g_imp_kwh[t]
        - export_prices[t] * g_exp_kwh[t]
        + sum(
            sc.degradation_cost_per_kwh * sc.charge_efficiency * charge_kwh[sc.device_id][t]
            for sc in storage_constraints
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
    total = 0.0
    for v in saving_per_step:
        total += v
        cum_saving.append(total)
    total_opt  = sum(opt_cost)
    total_base = sum(baseline_cost)
    total_save = sum(saving_per_step)
    total_deg  = sum(
        sc.degradation_cost_per_kwh * sc.charge_efficiency * charge_kwh[sc.device_id][t]
        for sc in storage_constraints
        for t in T
    )

    print()
    print(f"  Baseline cost    :  {total_base:+.3f} €  (no battery — grid + PV only)")
    print(f"  Optimised cost   :  {total_opt:+.3f} €  "
          f"(grid + PV + battery wear {total_deg:.3f} €)")
    if total_base:
        print(f"  Net saving       :  {total_save:+.3f} €  "
              f"({total_save / total_base * 100:+.1f} %)")

    # ── 12. Energy flows chart (Plotly) ──────────────────────────────────────
    g_imp_kw     = [v / step_h for v in g_imp_kwh]
    g_exp_kw     = [v / step_h for v in g_exp_kwh]
    charge_kw    = {bid: [v / step_h for v in vals] for bid, vals in charge_kwh.items()}
    discharge_kw = {bid: [v / step_h for v in vals] for bid, vals in discharge_kwh.items()}

    # Build SoC trajectories (length N+1: initial SoC + one value after each step)
    batteries_plot: dict[str, dict] = {}
    for sc in storage_constraints:
        soc_kwh = [initial_energy[sc.device_id]]
        for t in T:
            soc_kwh.append(pulp.value(variables["e"][(sc.device_id, t)]) or 0.0)
        batteries_plot[sc.device_id] = {
            "charge_kw":            charge_kw[sc.device_id],
            "discharge_kw":         discharge_kw[sc.device_id],
            "soc_pct":              [v / sc.capacity_kwh * 100 for v in soc_kwh],
            "capacity_kwh":         sc.capacity_kwh,
            "cost_basis_eur_per_kwh": cost_bases.get(sc.device_id, current_spot),
        }

    local_ts = [ts.astimezone() for ts in timestamps]
    fig = _vo._make_flows_figure(
        timestamps=local_ts,
        pv_kw=pv_kw,
        consumption_kw=consumption_kw,
        import_price_eur_per_kwh=prices,
        export_price_eur_per_kwh=export_prices,
        grid_import_kw=g_imp_kw,
        grid_export_kw=g_exp_kw,
        batteries=batteries_plot,
        saving_eur=saving_per_step,
        cumulative_saving_eur=cum_saving,
        step_minutes=step_min,
        title=f"Simulation: Energy Flows & Cost Impact  ({horizon_h} h horizon)",
    )
    if save_html:
        fig.write_html(save_html, include_plotlyjs="cdn")
        print(f"\n  Chart saved → {save_html}")
    else:
        fig.show()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_cost_basis(s: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for part in s.split(","):
        key, _, val = part.partition("=")
        result[key.strip()] = float(val.strip())
    return result


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
        "--price-tariff",
        metavar="ID",
        help="Pin the tariff used for electricity prices",
    )
    parser.add_argument(
        "--save-html",
        metavar="PATH",
        help="Save interactive chart to HTML instead of opening a browser tab",
    )
    parser.add_argument(
        "--cost-basis",
        metavar="K=V,...",
        help="Cost-basis overrides, e.g. sma_battery=0.12,zendure=0.08",
    )
    args = parser.parse_args()
    cost_basis = _parse_cost_basis(args.cost_basis) if args.cost_basis else None
    asyncio.run(_run(Path(args.config), args.price_tariff, args.save_html, cost_basis))


if __name__ == "__main__":
    _main()

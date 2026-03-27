"""Visualise the MILP optimizer plan using live data from config.yaml.

Loads the active ``config.yaml``, connects to the configured backends
(ioBroker / Home Assistant), and feeds real data into the optimizer:

  * Electricity prices   — from the configured tariff (Tibber, flat-rate, …)
  * PV generation        — from the configured forecast provider
  * Battery initial SoC  — read live from each storage device
  * StorageConstraints   — from the device declarations in config.yaml

Each data source prints "live" or "synthetic" so you can see immediately
what was actually retrieved.

Four-panel chart
----------------
    Panel 1 — Electricity price forecast (€/kWh)
    Panel 2 — PV generation forecast (kW)
    Panel 3 — Suggested battery charge / discharge power (kW)
    Panel 4 — Projected battery state of charge (%)

Usage::

    # Uses config.yaml next to this script's parent directory
    python scripts/visualize_optimizer.py

    # Explicit config path
    python scripts/visualize_optimizer.py --config /path/to/config.yaml

    # Save to file instead of opening a window
    python scripts/visualize_optimizer.py --save
    python scripts/visualize_optimizer.py --save --output plan.png

    # Use a specific tariff ID for electricity prices
    python scripts/visualize_optimizer.py --price-tariff household
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Insert src/ into the path so this script runs without installation
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

from energy_assistant.config.yaml import YamlConfigLoader
from energy_assistant.core.models import (
    DeviceRole,
    DeviceState,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
)
from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.loader.device_loader import build as build_from_config, build_device_forecasts
from energy_assistant.plugins import registry as plugin_registry
from energy_assistant.plugins._iobroker.pool import IoBrokerConnectionPool
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

# ── Visual constants ───────────────────────────────────────────────────────────

_PALETTE = ["#3a7d44", "#9b59b6", "#e07b39", "#1a75b5", "#c0392b"]
_PRICE_COLOR = "#e07b39"
_PV_COLOR = "#f5c518"


# ── UTC normalisation helper ──────────────────────────────────────────────────

def _to_utc(dt: datetime) -> datetime:
    """Return *dt* as a UTC-aware datetime.  Treats naive datetimes as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── Nearest-neighbour alignment ───────────────────────────────────────────────

def _align(points: list[ForecastPoint], timestamps: list[datetime]) -> list[float]:
    """Nearest-neighbour map of *points* onto *timestamps* (all UTC-aware)."""
    if not points:
        return [0.0] * len(timestamps)
    sorted_pts = sorted(points, key=lambda p: p.timestamp)
    return [
        min(sorted_pts, key=lambda p: abs((p.timestamp - ts).total_seconds())).value
        for ts in timestamps
    ]


# ── Time-step auto-detection ──────────────────────────────────────────────────

def _detect_step_minutes(*point_lists: list[ForecastPoint]) -> int:
    """Return the finest regular step found across all point lists.

    Scans consecutive gaps in each list, takes the minimum, then snaps
    to the nearest standard granularity (15 / 30 / 60 min).  Falls back
    to 60 when no data or fewer than two points are available.
    """
    min_gap = 60
    for pts in point_lists:
        if len(pts) < 2:
            continue
        sorted_pts = sorted(pts, key=lambda p: p.timestamp)
        for a, b in zip(sorted_pts, sorted_pts[1:]):
            gap = int((b.timestamp - a.timestamp).total_seconds() / 60)
            if gap > 0:
                min_gap = min(min_gap, gap)
    for standard in (15, 30, 60):
        if min_gap <= standard:
            return standard
    return 60


# ── Diagnostic output ─────────────────────────────────────────────────────────

def _print_diagnostics(
    raw_prices: list[ForecastPoint],
    raw_pv: list[ForecastPoint],
    step_min: int,
) -> None:
    """Print the first six raw points per source in UTC and local time."""
    local_tz = datetime.now().astimezone().tzinfo
    print(f"\n  Time step : {step_min} min   Local tz : {local_tz}")
    print("  Prices (UTC → local, first 6 points):")
    for pt in raw_prices[:6]:
        loc = pt.timestamp.astimezone(local_tz)
        print(f"    {pt.timestamp:%Y-%m-%dT%H:%M}Z → {loc:%H:%M %Z}  "
              f"{pt.value:.4f} €/kWh")
    if not raw_prices:
        print("    (none)")
    print("  PV forecast (UTC → local, first 6 points):")
    for pt in raw_pv[:6]:
        loc = pt.timestamp.astimezone(local_tz)
        print(f"    {pt.timestamp:%Y-%m-%dT%H:%M}Z → {loc:%H:%M %Z}  "
              f"{pt.value:.3f} kW")
    if not raw_pv:
        print("    (none)")


# ── Horizon inference ───────────────────────────────────────────────────────

def _infer_horizon(
    raw_prices: list[ForecastPoint],
    raw_pv: list[ForecastPoint],
    step_td: timedelta,
    now: datetime,
    cap: timedelta,
) -> timedelta:
    """Return the largest horizon fully covered by available data.

    Takes the latest timestamp across both data sources, snaps down to a
    whole number of *step_td* steps from *now*, and applies *cap* as an
    upper bound.  This means the optimizer always uses every real data
    point the sources provide, rather than being limited to a fixed window.
    """
    latest = now
    for pts in (raw_prices, raw_pv):
        if pts:
            candidate = max(p.timestamp for p in pts)
            if candidate > latest:
                latest = candidate
    raw_delta = latest - now
    capped = min(raw_delta, cap)
    step_secs = step_td.total_seconds()
    n_steps = max(1, int(capped.total_seconds() / step_secs))
    return step_td * n_steps


# ── Live data fetchers ────────────────────────────────────────────────────────

async def _fetch_raw_prices(
    tariffs: dict,
    price_tariff_id: str | None,
    horizon: timedelta,
) -> tuple[list[ForecastPoint], str]:
    """Return raw (un-aligned) UTC price points and a source description.

    Skips tariffs whose import price is entirely zero (e.g. a grid tariff
    configured with only export_price_eur_per_kwh).  Falls back to a
    synthetic hourly price profile.
    """
    ordered: list[tuple[str, Any]] = []
    if price_tariff_id and price_tariff_id in tariffs:
        ordered.append((price_tariff_id, tariffs[price_tariff_id]))
    for tid, t in tariffs.items():
        if not (price_tariff_id and tid == price_tariff_id):
            ordered.append((tid, t))

    for tid, tariff in ordered:
        try:
            sched = await tariff.price_schedule(horizon)
            if sched and any(tp.price_eur_per_kwh > 0.001 for tp in sched):
                pts = [
                    ForecastPoint(
                        timestamp=_to_utc(tp.timestamp),
                        value=tp.price_eur_per_kwh,
                    )
                    for tp in sched
                ]
                return pts, f"live — tariff '{tid}'"
        except Exception as exc:
            _log.debug("Tariff %r failed: %s", tid, exc)

    # Synthetic fallback — hourly so _detect_step_minutes returns 60
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    shape = [
        0.22, 0.20, 0.19, 0.18, 0.18, 0.21,
        0.26, 0.30, 0.34, 0.28, 0.24, 0.18,
        0.15, 0.13, 0.12, 0.14, 0.19, 0.27,
        0.35, 0.38, 0.36, 0.32, 0.28, 0.25,
    ]
    hours = int(horizon.total_seconds() / 3600) + 1
    return (
        [
            ForecastPoint(
                timestamp=now + timedelta(hours=i),
                value=shape[(now.hour + i) % 24],
            )
            for i in range(hours)
        ],
        "synthetic (no live tariff available)",
    )


async def _fetch_raw_pv(
    providers: list,
    horizon: timedelta,
) -> tuple[list[ForecastPoint], str]:
    """Return raw (un-aligned) UTC PV forecast points and a source description.

    All timestamps are normalised to UTC.  Falls back to a synthetic
    hourly Gaussian PV profile.
    """
    for provider in providers:
        if getattr(provider, "quantity", None) != ForecastQuantity.PV_GENERATION:
            continue
        try:
            pts = await provider.get_forecast(horizon)
            if pts:
                utc_pts = [
                    ForecastPoint(timestamp=_to_utc(p.timestamp), value=p.value)
                    for p in pts
                ]
                return utc_pts, f"live — {type(provider).__name__}"
        except Exception as exc:
            _log.debug("PV forecast provider failed: %s", exc)

    # Synthetic fallback — hourly Gaussian profile
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hours = int(horizon.total_seconds() / 3600) + 1
    return (
        [
            ForecastPoint(
                timestamp=now + timedelta(hours=i),
                value=max(
                    0.0,
                    6.5 * np.exp(-0.5 * (((now.hour + i) % 24 - 12.5) / 2.8) ** 2),
                ),
            )
            for i in range(hours)
        ],
        "synthetic (no live PV forecast available)",
    )


async def _fetch_soc(device: Any) -> float | None:
    try:
        state: DeviceState = await device.get_state()
        if state.available and state.soc_pct is not None:
            return state.soc_pct
    except Exception as exc:
        _log.debug("get_state() failed for %r: %s", device.device_id, exc)
    return None


# ── Build forecast providers from config ──────────────────────────────────────

async def _fetch_raw_consumption(
    device_forecasts: list,
    timestamps: list[datetime],
    horizon: timedelta,
) -> tuple[list[ForecastPoint], str]:
    """Return per-step consumption forecast values as ForecastPoints.

    Sums the consumption forecasts of all consumer devices that declared
    a ``forecast:`` section (e.g. ``static_profile`` on the baseline device
    or heatpump).  Each device carries its own tariff reference so future
    versions can weight the cost correctly per load.
    """
    values = [0.0] * len(timestamps)
    sources: list[str] = []

    for provider in device_forecasts:
        if getattr(provider, "quantity", None) != ForecastQuantity.CONSUMPTION:
            continue
        try:
            raw = await provider.get_forecast(horizon)
            aligned = _align(raw, timestamps)
            for i, v in enumerate(aligned):
                values[i] += v
            sources.append(type(provider).__name__)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Consumption forecast provider failed: %s", exc)

    pts = [ForecastPoint(timestamp=ts, value=v) for ts, v in zip(timestamps, values)]
    desc = ", ".join(sources) if sources else "no device forecasts configured"
    return pts, desc


def _build_forecast_providers(app_config: Any, ctx: BuildContext) -> list:
    providers = []
    for fid, cfg in app_config.forecasts.items():
        try:
            p = plugin_registry.build_forecast(fid, cfg, ctx)
            if p is not None:
                providers.append(p)
        except Exception as exc:
            _log.warning("Could not build forecast '%s': %s", fid, exc)
    return providers


# ── Main async routine ────────────────────────────────────────────────────────

async def _run(
    config_path: Path,
    price_tariff_id: str | None,
    save_path: str | None,
    cost_basis_override: dict[str, float] | None = None,
) -> None:
    """Run the optimizer and render the plan chart.

    Parameters
    ----------
    cost_basis_override:
        Optional dict mapping device_id → cost basis (€/kWh).  When provided,
        these values are used instead of the live spot price.  Useful for
        experimenting in a notebook: e.g. ``{"sma_battery": 0.22, "zendure": 0.18}``.
        Any device not listed falls back to the current spot price.
    """
    # 1. Load config
    app_config = YamlConfigLoader(config_path).load()

    # 2. Build devices + tariffs via the standard loader
    device_registry, tariffs, _topology = build_from_config(app_config)

    # 3. Build a shared BuildContext for forecast providers
    iobroker_pool = None
    if app_config.backends.iobroker:
        iobroker_pool = IoBrokerConnectionPool()
    ctx = BuildContext(
        backends=app_config.backends,
        iobroker_pool=iobroker_pool,
        ha_client=None,
    )
    forecast_providers = _build_forecast_providers(app_config, ctx)
    device_forecasts   = build_device_forecasts(app_config, ctx)

    # 4. Planning horizon cap from config (acts as an upper bound, not a fixed target)
    horizon_cap_h = int(app_config.optimizer.get("horizon_hours", 48))
    horizon_cap = timedelta(hours=horizon_cap_h)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    # 5. Fetch raw data with the cap as the requested window.
    #    Sources (Tibber, PV provider) return whatever they have — often more
    #    or less than the cap.  We measure the actual span afterwards.
    raw_prices, price_src = await _fetch_raw_prices(tariffs, price_tariff_id, horizon_cap)
    print(f"  Prices       : {price_src}")
    raw_pv, pv_src = await _fetch_raw_pv(forecast_providers, horizon_cap)
    print(f"  PV forecast  : {pv_src}")

    # 6. Detect step size, then infer the actual usable horizon from data extent
    step_min = _detect_step_minutes(raw_prices, raw_pv)
    step_td = timedelta(minutes=step_min)
    horizon = _infer_horizon(raw_prices, raw_pv, step_td, now, cap=horizon_cap)
    horizon_h = int(horizon.total_seconds() / 3600)
    n_steps = int(horizon / step_td)
    timestamps = [now + step_td * i for i in range(n_steps)]
    print(f"  Time step    : {step_min} min  →  {n_steps} steps over {horizon_h} h")

    # Align raw data onto the unified time grid (nearest-neighbour)
    prices = _align(raw_prices, timestamps)
    pv_kw = _align(raw_pv, timestamps)
    _print_diagnostics(raw_prices, raw_pv, step_min)

    # 7. Storage devices + live SoC
    storage_devices = device_registry.by_role(DeviceRole.STORAGE)
    if not storage_devices:
        print("  WARNING: no storage devices found — check config.yaml")

    storage_constraints: list[StorageConstraints] = []
    device_states: dict[str, DeviceState] = {}

    for device in storage_devices:
        sc: StorageConstraints | None = getattr(device, "storage_constraints", None)
        if sc is None:
            _log.warning("Device %r has no storage_constraints — skipping", device.device_id)
            continue
        storage_constraints.append(sc)

        soc = await _fetch_soc(device)
        if soc is not None:
            device_states[device.device_id] = DeviceState(device_id=device.device_id, soc_pct=soc)
            print(f"  SoC {device.device_id:20s}: live ({soc:.1f} %)")
        else:
            default_soc = (sc.min_soc_pct + sc.max_soc_pct) / 2.0
            device_states[device.device_id] = DeviceState(
                device_id=device.device_id, soc_pct=default_soc
            )
            print(f"  SoC {device.device_id:20s}: synthetic (fallback {default_soc:.0f} %)")

    # 7b. Initialise cost ledger
    # Priority: cost_basis_override > current spot price (conservative fallback).
    current_spot = prices[0] if prices else 0.25
    overrides = cost_basis_override or {}
    ledger = BatteryCostLedger()
    for sc in storage_constraints:
        state = device_states.get(sc.device_id)
        stored = sc.capacity_kwh * (state.soc_pct or 0.0) / 100.0
        if sc.device_id in overrides:
            basis = overrides[sc.device_id]
            source = "manual override"
        else:
            basis = current_spot
            source = "current spot"
        ledger.initialise(sc.device_id, stored_energy_kwh=stored, cost_basis_eur_per_kwh=basis)
        print(f"  CostBasis {sc.device_id:16s}: {basis:.4f} €/kWh "
              f"({source}, {stored:.1f} kWh stored)")

    # 8. Build OptimizationContext
    price_fc = [ForecastPoint(timestamp=ts, value=p) for ts, p in zip(timestamps, prices)]
    pv_fc = [ForecastPoint(timestamp=ts, value=v) for ts, v in zip(timestamps, pv_kw)]

    baseline_load_kw = float(app_config.optimizer.get("baseline_load_kw", 0.0))
    if baseline_load_kw > 0:
        _log.warning(
            "config.yaml: optimizer.baseline_load_kw=%.2f is deprecated — "
            "add a 'baseline' generic_consumer device with a static_profile forecast instead.",
            baseline_load_kw,
        )
    consumption_fc, consumption_src = await _fetch_raw_consumption(
        device_forecasts, timestamps, horizon
    )
    # Legacy: add deprecated baseline on top of device forecasts
    if baseline_load_kw > 0:
        consumption_fc = [
            ForecastPoint(timestamp=fp.timestamp, value=fp.value + baseline_load_kw)
            for fp in consumption_fc
        ]
    print(f"  Consumption  : {consumption_src}")

    context = OptimizationContext(
        device_states=device_states,
        storage_constraints=storage_constraints,
        tariffs=tariffs,
        forecasts={
            ForecastQuantity.PRICE: price_fc,
            ForecastQuantity.PV_GENERATION: pv_fc,
            ForecastQuantity.CONSUMPTION: consumption_fc,
        },
        horizon=horizon,
        battery_cost_basis=ledger.all_cost_bases(),
    )

    # 9. Optimise — use the auto-detected step size
    plan = await MilpHigsOptimizer(step_minutes=step_min).optimize(context)

    # 10. Console summary
    print("\n  Battery schedule:")
    for sc in storage_constraints:
        counts = Counter(i.mode for i in plan.intents if i.device_id == sc.device_id)
        print("    {:20s}  {}".format(
            sc.device_id,
            "  ".join(f"{m}: {n}h" for m, n in counts.items()),
        ))

    # 11. Plot
    series = _extract_series(context, plan, timestamps, step_min)
    _plot(context, plan, series, price_src, pv_src, save_path,
          tariffs=tariffs, step_min=step_min)


# ── Post-processing ───────────────────────────────────────────────────────────

def _extract_series(
    context: OptimizationContext,
    plan: Any,
    timestamps: list[datetime],
    step_min: int = 60,
) -> dict:
    price_map = {fp.timestamp: fp.value
                 for fp in context.forecasts.get(ForecastQuantity.PRICE, [])}
    pv_map = {fp.timestamp: fp.value
              for fp in context.forecasts.get(ForecastQuantity.PV_GENERATION, [])}
    consumption_map = {fp.timestamp: fp.value
                       for fp in context.forecasts.get(ForecastQuantity.CONSUMPTION, [])}

    intent_power: dict[str, dict[datetime, float]] = {
        sc.device_id: {} for sc in context.storage_constraints
    }
    for intent in plan.intents:
        if intent.mode == "grid_fill":
            kw = (intent.max_power_w or 0.0) / 1000.0
        elif intent.mode == "discharge":
            kw = (intent.min_power_w or 0.0) / 1000.0
        else:
            kw = 0.0
        intent_power[intent.device_id][intent.timestep] = kw

    step_h = step_min / 60.0
    soc_trajectories: dict[str, list[float]] = {}
    for sc in context.storage_constraints:
        bid = sc.device_id
        e = sc.capacity_kwh * (context.device_states[bid].soc_pct or 0.0) / 100.0
        soc = [e / sc.capacity_kwh * 100.0]
        for ts in timestamps:
            kw = intent_power[bid].get(ts, 0.0)
            if kw > 0:
                e += kw * step_h * sc.charge_efficiency
            elif kw < 0:
                e += kw * step_h / sc.discharge_efficiency
            e = max(sc.capacity_kwh * sc.min_soc_pct / 100.0,
                    min(sc.capacity_kwh * sc.max_soc_pct / 100.0, e))
            soc.append(e / sc.capacity_kwh * 100.0)
        soc_trajectories[bid] = soc

    return {
        "timestamps": timestamps,
        "prices": [price_map.get(ts, 0.0) for ts in timestamps],
        "pv": [pv_map.get(ts, 0.0) for ts in timestamps],
        "consumption": [consumption_map.get(ts, 0.0) for ts in timestamps],
        "intent_power": intent_power,
        "soc": soc_trajectories,
    }


# ── Plotting ───────────────────────────────────────────────────────────────────

def _plot(
    context: OptimizationContext,
    plan: Any,
    series: dict,
    price_src: str,
    pv_src: str,
    save_path: str | None,
    tariffs: dict | None = None,
    step_min: int = 60,
) -> None:
    has_batteries = bool(context.storage_constraints)
    n_panels = 4 if has_batteries else 2
    height_ratios = [1.0, 1.2, 1.5, 1.2][:n_panels]

    fig, axes = plt.subplots(
        n_panels, 1, figsize=(14, 3.0 * n_panels + 0.8), sharex=True,
        gridspec_kw={"height_ratios": height_ratios},
    )
    if n_panels == 1:
        axes = [axes]

    local_tz = datetime.now().astimezone().tzinfo
    now_local = datetime.now(timezone.utc).astimezone(local_tz)
    now_str = now_local.strftime("%Y-%m-%d %H:%M %Z")
    fig.suptitle(f"Energy Optimizer — planned battery schedule  ({now_str})",
                 fontsize=12, fontweight="bold", y=0.999)

    xs = series["timestamps"]

    # Panel 1 — Prices
    ax = axes[0]
    ax.step(xs, series["prices"], where="post", color=_PRICE_COLOR, linewidth=2,
            label="Spot price")
    ax.fill_between(xs, series["prices"], step="post", alpha=0.18, color=_PRICE_COLOR)
    ax.set_ylabel("Price (€/kWh)", fontsize=9)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
    ax.set_title(f"Electricity price forecast  [{price_src}]",
                 fontsize=9, loc="left", color="#444")
    ax.grid(axis="y", linestyle=":", alpha=0.5)
    # Reference lines for flat-rate tariffs (e.g. heatpump, export)
    flat_ref_colors = ["#c0392b", "#2ecc71", "#8e44ad"]
    ref_idx = 0
    for tid, tariff in (tariffs or {}).items():
        if isinstance(tariff, FlatRateTariff):
            if tariff._import_price > 0.001:
                c = flat_ref_colors[ref_idx % len(flat_ref_colors)]
                ax.axhline(tariff._import_price, linestyle="--", linewidth=1.1,
                           color=c, alpha=0.75,
                           label=f"{tid}: {tariff._import_price:.3f} €/kWh")
                ref_idx += 1
            if tariff._export_price > 0.001:
                c = flat_ref_colors[ref_idx % len(flat_ref_colors)]
                ax.axhline(tariff._export_price, linestyle=":", linewidth=1.0,
                           color=c, alpha=0.65,
                           label=f"{tid} export: {tariff._export_price:.3f} €/kWh")
                ref_idx += 1
    if ref_idx > 0:
        ax.legend(fontsize=8, loc="upper right")
    ax.set_ylim(bottom=0)

    # Panel 2 — PV generation + estimated load
    ax = axes[1]
    ax.fill_between(xs, series["pv"], step="post", alpha=0.35, color=_PV_COLOR)
    ax.step(xs, series["pv"], where="post", color=_PV_COLOR, linewidth=1.8,
            label="PV generation")
    consumption = series.get("consumption", [])
    if any(v > 0.0 for v in consumption):
        ax.step(xs, consumption, where="post", color="#e74c3c", linewidth=1.6,
                linestyle="--", label="Baseline load (est.)")
    ax.set_ylabel("Power (kW)", fontsize=9)
    ax.set_title(f"PV generation & baseline load  [{pv_src}]",
                 fontsize=9, loc="left", color="#444")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(axis="y", linestyle=":", alpha=0.5)
    ax.set_ylim(bottom=0)

    if has_batteries:
        # Panel 3 — Battery power
        ax = axes[2]
        n_dev = len(context.storage_constraints)
        step_h = step_min / 60.0
        slot_h = step_h * 0.80  # total bar-group width as fraction of one time step
        bar_w = timedelta(hours=slot_h / max(n_dev, 1))

        for i, sc in enumerate(context.storage_constraints):
            bid = sc.device_id
            color = _PALETTE[i % len(_PALETTE)]
            offset = timedelta(hours=(i - n_dev / 2 + 0.5) * slot_h / max(n_dev, 1))
            bxs = [ts + offset for ts in xs]
            vals = [series["intent_power"][bid].get(ts, 0.0) for ts in xs]
            pos = [v if v > 0 else 0.0 for v in vals]
            neg = [v if v < 0 else 0.0 for v in vals]
            ax.bar(bxs, pos, width=bar_w, color=color, alpha=0.85,
                   label=f"{bid} ▲", align="center")
            ax.bar(bxs, neg, width=bar_w, color=color, alpha=0.45,
                   label=f"{bid} ▼", hatch="///", align="center")

        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_ylabel("Power (kW)\n+ charge  − discharge", fontsize=9)
        ax.set_title("Suggested battery charge / discharge",
                     fontsize=9, loc="left", color="#444")
        ax.legend(fontsize=8, loc="upper right", ncol=2)
        ax.grid(axis="y", linestyle=":", alpha=0.5)

        # Panel 4 — SoC
        ax = axes[3]
        # One SoC value before the first step + one after each step
        soc_xs = xs + [xs[-1] + timedelta(minutes=step_min)]
        for i, sc in enumerate(context.storage_constraints):
            bid = sc.device_id
            color = _PALETTE[i % len(_PALETTE)]
            ax.plot(soc_xs, series["soc"][bid], color=color, linewidth=2,
                    marker="o", markersize=3, label=bid)
            ax.axhline(sc.min_soc_pct, color=color, linewidth=0.7,
                       linestyle="--", alpha=0.55, label=f"{bid} min")
            ax.axhline(sc.max_soc_pct, color=color, linewidth=0.7,
                       linestyle=":",  alpha=0.55, label=f"{bid} max")

        ax.set_ylabel("SoC (%)", fontsize=9)
        ax.set_title("Projected state of charge  [projected from initial SoC + schedule]",
                     fontsize=9, loc="left", color="#444")
        ax.set_ylim(0, 110)
        ax.legend(fontsize=7, loc="upper right", ncol=3)
        ax.grid(axis="y", linestyle=":", alpha=0.5)

    # X-axis (shared) — local time; tick interval scales to step resolution
    tick_h = 1 if step_min <= 30 else 2
    axes[-1].xaxis.set_major_locator(mdates.HourLocator(interval=tick_h))
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=local_tz))
    axes[-1].set_xlabel(f"Time ({local_tz})", fontsize=9)
    plt.setp(axes[-1].xaxis.get_majorticklabels(), rotation=0, ha="center")

    fig.tight_layout(rect=[0, 0, 1, 0.998])

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"\n  Saved → {save_path}")
    else:
        plt.show()


# ── Entry point ───────────────────────────────────────────────────────────────

def _default_config() -> Path:
    return Path(__file__).resolve().parents[1] / "config.yaml"


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Visualise MILP optimizer from live config.yaml data"
    )
    parser.add_argument("--config", default=str(_default_config()),
                        help="Path to config.yaml (default: <repo>/config.yaml)")
    parser.add_argument("--price-tariff", default=None, dest="price_tariff",
                        help="Tariff ID to use for electricity prices (default: first available)")
    parser.add_argument("--save", action="store_true",
                        help="Save chart to a file instead of opening a window")
    parser.add_argument("--output", default="optimizer_plan.png",
                        help="Output filename when --save is used (default: optimizer_plan.png)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    print(f"\nConfig : {config_path}")
    print("Sources:")
    await _run(config_path, args.price_tariff, args.output if args.save else None)


if __name__ == "__main__":
    asyncio.run(_main())

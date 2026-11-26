"""Backtest a ``type: learned`` consumption forecast against its own history.

Fetches the same Home Assistant statistics the server trains on (honouring
``exclude`` / ``exclude_when``), then walks forward day by day: the model is
refitted weekly on everything before the day being predicted, using the
day's actual mean temperature (i.e. assuming a perfect weather forecast).

Prints the daily-energy error of the learned model next to two naive
references and, when configured, the forecast's ``fallback`` profile.

Usage:
    python scripts/backtest_learned.py heatpump_meter
    python scripts/backtest_learned.py heatpump_meter --config config.yaml --warmup 60
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from datetime import datetime, timedelta

from nornwatt.config.yaml import YamlConfigLoader
from nornwatt.core.learned_model import fit
from nornwatt.loader.device_loader import make_build_context
from nornwatt.plugins import registry


async def _fallback_kwh(provider, day) -> float:
    """Daily kWh the fallback profile predicts for *day* (weekday-dependent)."""
    return sum(provider._power_for(day.weekday(), h) for h in range(24))  # noqa: SLF001


async def main(device_id: str, config_path: str, warmup: int) -> None:
    cfg = YamlConfigLoader(config_path).load()
    spec = cfg.devices.get(device_id)
    forecast_cfg = (spec.forecast if spec is not None else None) or {}
    if forecast_cfg.get("type") != "learned":
        raise SystemExit(f"device {device_id!r} has no 'type: learned' forecast")

    ctx = make_build_context(cfg)
    provider = registry.build_forecast(f"{device_id}_forecast", forecast_cfg, ctx)
    try:
        days = await provider.fetch_days()
    finally:
        if ctx.ha_client is not None:
            await ctx.ha_client.close()

    print(f"{len(days)} usable days  {days[0].day} -> {days[-1].day}")
    full = fit(days)
    if full is None:
        raise SystemExit("not enough history to fit a model")
    if full.mode == "temperature":
        side = "below" if full.direction == "heating" else "above"
        print(
            f"full fit: {full.floor_kwh:.2f} kWh/day + {full.slope_kwh_per_k:.2f} kWh/K "
            f"{side} {full.base_temp:.1f} °C   R²={full.r2:.3f}  MAE={full.mae_kwh:.2f}  "
            f"carry-forward={full.rho:.2f}  outliers dropped={full.n_outliers}"
        )
        # Temperature mode trains on days with a known temperature only.
        days = [d for d in days if d.temp is not None]
    else:
        print(f"full fit: level {full.level_kwh}  MAE={full.mae_kwh:.2f}  carry-forward={full.rho:.2f}")

    fallback = getattr(provider, "_fallback", None)
    errors: dict[str, list[float]] = {}
    active: list[bool] = []
    actual: list[float] = []
    model = None
    for i in range(warmup, len(days)):
        if model is None or (i - warmup) % 7 == 0:
            model = fit(days[:i]) or model
        if model is None:
            continue
        d = days[i]
        # The server refits several times a day, so its carry-forward always
        # reflects the latest completed day — mirror that between refits.
        prev = days[i - 1]
        model.observe(prev.day, prev.kwh, prev.temp)
        errors.setdefault("learned", []).append(abs(model.daily_kwh(d.day, d.temp) - d.kwh))
        errors.setdefault("previous usable day", []).append(abs(days[i - 1].kwh - d.kwh))
        errors.setdefault("7 usable days earlier", []).append(abs(days[i - 7].kwh - d.kwh))
        if fallback is not None and hasattr(fallback, "_power_for"):
            errors.setdefault("fallback profile", []).append(
                abs(await _fallback_kwh(fallback, d.day) - d.kwh)
            )
        active.append(full.mode == "temperature" and d.temp is not None and full.hinge(d.temp) > 0)
        actual.append(d.kwh)

    if not actual:
        raise SystemExit(f"fewer than {warmup} days of warm-up history — lower --warmup")

    n_active = sum(active)
    print(
        f"\nwalk-forward over {len(actual)} days "
        f"(mean {statistics.fmean(actual):.1f} kWh/day; {n_active} with the temperature term active)"
    )
    print(f"{'model':24s} {'MAE all':>8s} {'MAE active':>11s}")
    for name, errs in errors.items():
        act = [e for e, a in zip(errs, active) if a]
        print(
            f"{name:24s} {statistics.fmean(errs):8.2f} "
            f"{(f'{statistics.fmean(act):11.2f}' if act else '          -')}"
        )

    # Hourly shape for a typical active and idle day.
    if full.mode == "temperature":
        today = datetime.now().date()
        cold = full.base_temp - 10 if full.direction == "heating" else full.base_temp + 10
        warm = full.base_temp + 5 if full.direction == "heating" else full.base_temp - 5
        print("\npredicted W by local hour (weekday)   active   idle")
        day = today - timedelta(days=today.weekday())  # a Monday
        for h in range(24):
            ts = datetime(day.year, day.month, day.day, h)
            # ignore carry-forward: show the plain curve
            a = full.base_kwh(day, cold) * full.shape(day, cold)[h] * 1000
            b = full.base_kwh(day, warm) * full.shape(day, warm)[h] * 1000
            print(f"  {ts:%H}:00 {a:28.0f} {b:6.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device_id")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--warmup", type=int, default=60, help="days of history before the first prediction")
    args = ap.parse_args()
    asyncio.run(main(args.device_id, args.config, args.warmup))

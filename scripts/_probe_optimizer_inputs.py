"""Show what the optimizer actually sees for each 15-min step: net_load, import price, export price."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from energy_assistant.config.yaml import YamlConfigLoader
from energy_assistant.loader.device_loader import build as build_from_config
from energy_assistant.core.models import DeviceRole, ForecastPoint, ForecastQuantity
from energy_assistant.plugins import registry as plugin_registry
from energy_assistant.plugins._iobroker.pool import IoBrokerConnectionPool
from energy_assistant.core.plugin_registry import BuildContext


async def main() -> None:
    app_config = YamlConfigLoader(Path(__file__).parents[1] / "config.yaml").load()
    device_registry, tariffs, _ = build_from_config(app_config)
    pool = IoBrokerConnectionPool()
    ctx = BuildContext(backends=app_config.backends, iobroker_pool=pool, ha_client=None)

    providers = []
    for fid, cfg in app_config.forecasts.items():
        p = plugin_registry.build_forecast(fid, cfg, ctx)
        if p:
            providers.append(p)

    horizon = timedelta(hours=24)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    step_td = timedelta(minutes=15)
    n_steps = int(horizon / step_td)
    timestamps = [now + step_td * i for i in range(n_steps)]
    LTZ = datetime.now().astimezone().tzinfo

    # Import prices
    sched = await tariffs["household"].price_schedule(horizon)
    import_map = {tp.timestamp: tp.price_eur_per_kwh for tp in sched}
    # Export prices
    exp_sched = await tariffs["grid"].export_price_schedule(horizon)
    export_map = {tp.timestamp: tp.price_eur_per_kwh for tp in exp_sched}
    # PV
    pv_pts = await providers[0].get_forecast(horizon)
    pv_map = {}
    for p in pv_pts:
        pv_map[p.timestamp] = p.value
    def nearest(m, ts):
        if not m: return 0.0
        return min(m.items(), key=lambda x: abs((x[0]-ts).total_seconds()))[1]

    baseline = float(app_config.optimizer.get("baseline_load_kw", 0.0))
    print(f"{'UTC':5}  {'CET':5}  {'imp':>6}  {'exp':>6}  {'pv':>6}  {'load':>5}  {'net':>7}  {'val_keep':>9}")
    print("-" * 70)
    for ts in timestamps:
        imp = nearest(import_map, ts)
        exp = nearest(export_map, ts)
        pv  = nearest(pv_map, ts)
        net = (baseline - pv) * (15/60)   # net_load kWh per step
        # value of keeping 1 kWh vs exporting it now
        val_keep = imp - exp   # saving import at imp vs earning exp now
        loc = ts.astimezone(LTZ)
        surplus = "PV" if net < 0 else "  "
        if loc.hour >= 13:   # only show afternoon onwards
            print(f"{ts:%H:%M}  {loc:%H:%M}  {imp:.3f}  {exp:.3f}  {pv:6.2f}  {baseline:.2f}  {net:7.3f}  {val_keep:9.3f}  {surplus}")
        if loc.hour >= 23:
            break


asyncio.run(main())

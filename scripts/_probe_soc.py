"""Probe live SoC vs configured max_soc_pct for all storage devices."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from energy_assistant.config.yaml import YamlConfigLoader
from energy_assistant.loader.device_loader import build as build_from_config
from energy_assistant.core.models import DeviceRole


async def main() -> None:
    app_config = YamlConfigLoader(Path(__file__).parents[1] / "config.yaml").load()
    device_registry, _tariffs, _ = build_from_config(app_config)

    for dev in device_registry.by_role(DeviceRole.STORAGE):
        sc = getattr(dev, "storage_constraints", None)
        state = await dev.get_state()
        if sc and state:
            e_init = sc.capacity_kwh * (state.soc_pct or 0) / 100
            e_max  = sc.capacity_kwh * sc.max_soc_pct / 100
            forced = max(0.0, (e_init - e_max) * sc.discharge_efficiency)
            print(f"{dev.device_id}:")
            print(f"  live_soc={state.soc_pct}%   max_soc_pct={sc.max_soc_pct}%   capacity={sc.capacity_kwh} kWh")
            print(f"  e_init={e_init:.2f} kWh   e_max={e_max:.2f} kWh")
            print(f"  {'*** FORCED DISCHARGE: ' + f'{forced:.2f} kWh to reach e_max ***' if forced > 0.01 else 'OK — e_init <= e_max'}")


asyncio.run(main())

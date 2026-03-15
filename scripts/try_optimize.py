"""
End-to-end smoke test for the MILP battery optimizer.

Connects to a real ioBroker instance, reads:
  - Current battery state (Zendure SolarFlow)
  - PV forecast (pvforecast adapter)
  - Tibber electricity prices (tibberlink adapter)

Solves the 24-hour MILP scheduling problem and prints the resulting
charge/discharge plan WITHOUT applying it to the device.

Run with:
    python scripts/try_optimize.py

Device configuration is loaded from config.yaml (gitignored).
Copy config.yaml.example → config.yaml and fill in your values.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from energy_manager.core.models import ForecastQuantity
from energy_manager.core.optimizer import OptimizationContext
from energy_manager.plugins._iobroker.client import IoBrokerClient
from energy_manager.plugins.milp.optimizer import MILPOptimizer
from energy_manager.plugins.pvforecast_iobroker.forecast import PVForecastIoBrokerForecast
from energy_manager.plugins.sma_modbus_iobroker.device import SMAModbusIoBrokerDevice
from energy_manager.plugins.tibber_iobroker.tariff import TibberIoBrokerTariff
from energy_manager.plugins.zendure_iobroker.device import ZendureIoBrokerDevice

_ROOT = Path(__file__).parent.parent
with open(_ROOT / "config.yaml") as _f:
    _cfg = yaml.safe_load(_f)

HOST = _cfg["iobroker"]["host"]
PORT = int(_cfg["iobroker"]["port"])
HOME_ID = _cfg["tibber_iobroker"]["home_id"]
HUB_ID = _cfg["zendure_iobroker"]["hub_id"]
DEVICE_SERIAL = _cfg["zendure_iobroker"]["device_serial"]
CAPACITY_KWH = float(_cfg["zendure_iobroker"]["capacity_kwh"])
MAX_CHARGE_KW = float(_cfg["zendure_iobroker"]["max_charge_kw"])
MAX_DISCHARGE_KW = float(_cfg["zendure_iobroker"]["max_discharge_kw"])

_opt = _cfg.get("milp", {})
HORIZON_H = int(_opt.get("horizon_hours", 24))
BASELINE_LOAD_KW = float(_opt.get("baseline_load_kw", 0.3))
FEED_IN_EUR_PER_KWH = float(_opt.get("feed_in_eur_per_kwh", 0.0))
_pv = _cfg.get("pvforecast_iobroker", {})
PVFORECAST_PLANT_ID = _pv.get("plant_id", "pv")
PVFORECAST_TZ = ZoneInfo(_pv.get("timezone", "Europe/Berlin"))


async def main() -> None:
    print(f"Connecting to ioBroker at {HOST}:{PORT} …\n")

    async with IoBrokerClient(HOST, PORT) as client:
        # --- Instantiate all data sources -----------------------------------
        battery = ZendureIoBrokerDevice(
            device_id="zendure",
            client=client,
            hub_id=HUB_ID,
            device_serial=DEVICE_SERIAL,
            capacity_kwh=CAPACITY_KWH,
            max_charge_kw=MAX_CHARGE_KW,
            max_discharge_kw=MAX_DISCHARGE_KW,
        )
        _sma_cfg = _cfg.get("sma_modbus_iobroker") or {}
        sma_battery = SMAModbusIoBrokerDevice(
            device_id="sma_battery",
            client=client,
            **{k: v for k, v in _sma_cfg.items() if k.startswith("oid_")},
        )
        tariff = TibberIoBrokerTariff(
            tariff_id="tibber",
            client=client,
            home_id=HOME_ID,
            include_tomorrow=True,
        )
        pv_forecast = PVForecastIoBrokerForecast(
            client=client,
            plant_id=PVFORECAST_PLANT_ID,
            tz=PVFORECAST_TZ,
        )

        # --- Fetch current state --------------------------------------------
        battery_state = await battery.get_state()
        sma_state = await sma_battery.get_state()
        print(f"Zendure SoC:        {battery_state.soc_pct:.0f} %")
        print(f"Zendure power:      {battery_state.power_w:+.0f} W  "
              f"({'charging' if battery_state.power_w < 0 else 'discharging' if battery_state.power_w > 0 else 'idle'})")
        print(f"Solar input:        {battery_state.extra.get('solar_input_w', 0):.0f} W")
        sma_soc = f"{sma_state.soc_pct:.0f} %" if sma_state.soc_pct is not None else "n/a"
        sma_max_cha = sma_state.extra.get("max_charge_w")
        sma_max_dsch = sma_state.extra.get("max_discharge_w")
        sma_limits = (
            f"max charge {sma_max_cha:.0f} W / max discharge {sma_max_dsch:.0f} W"
            if sma_max_cha and sma_max_dsch else "limits unknown"
        )
        print(f"SMA battery SoC:    {sma_soc}  (read-only, not scheduled)")
        print(f"SMA battery power:  {sma_state.power_w:+.0f} W  [{sma_limits}]\n")

        # --- Fetch forecasts & tariff schedule ------------------------------
        horizon = timedelta(hours=HORIZON_H)
        pv_points = await pv_forecast.get_forecast(horizon)
        tariff_schedule = await tariff.price_schedule(horizon)

        print(f"PV forecast:        {len(pv_points)} hourly slots over next {HORIZON_H} h")
        if pv_points:
            peak = max(pv_points, key=lambda p: p.value)
            local_peak = peak.timestamp.astimezone()
            print(f"  Peak:             {peak.value:.0f} W at {local_peak.strftime('%H:%M')}")
        print(f"Tariff slots:       {len(tariff_schedule)} over next {HORIZON_H} h")
        if tariff_schedule:
            cheapest = min(tariff_schedule, key=lambda p: p.price_eur_per_kwh)
            priciest = max(tariff_schedule, key=lambda p: p.price_eur_per_kwh)
            print(f"  Cheapest:         {cheapest.price_eur_per_kwh:.4f} EUR/kWh at "
                  f"{cheapest.timestamp.astimezone().strftime('%H:%M')}")
            print(f"  Most expensive:   {priciest.price_eur_per_kwh:.4f} EUR/kWh at "
                  f"{priciest.timestamp.astimezone().strftime('%H:%M')}")
        print()

        # --- Build optimization context -------------------------------------
        # Only controllable devices go into storage_constraints.
        # sma_battery returns None for storage_constraints — excluded automatically.
        controllable = [d.storage_constraints for d in [battery, sma_battery] if d.storage_constraints is not None]
        context = OptimizationContext(
            device_states={"zendure": battery_state, "sma_battery": sma_state},
            storage_constraints=controllable,
            tariffs={"default": tariff},
            forecasts={ForecastQuantity.PV_GENERATION: pv_points},
            horizon=horizon,
        )

        # --- Configure and run MILP optimizer -------------------------------
        optimizer = MILPOptimizer(
            baseline_load_kw=BASELINE_LOAD_KW,
            feed_in_price_eur_per_kwh=FEED_IN_EUR_PER_KWH,
            solver_msg=False,
        )

        print("Running MILP optimizer …")
        plan = await optimizer.optimize(context)
        print(f"Solved: {len(plan.actions)} scheduled actions over {plan.horizon_hours} h\n")

        # --- Display plan ---------------------------------------------------
        print(f"{'Time':>6}  {'Action':>12}  {'(W)':>6}  {'Visual':}")
        print("-" * 60)
        for action in plan.actions:
            local = action.scheduled_at.astimezone()
            watts = action.value
            if watts < -50:
                label = "CHARGE"
                bar = "◀" * min(int(-watts / 100), 12)
                color = ""
            elif watts > 50:
                label = "DISCHARGE"
                bar = "▶" * min(int(watts / 100), 12)
                color = ""
            else:
                label = "idle"
                bar = "·"
                color = ""
            print(f"  {local.strftime('%H:%M')}  {label:>12}  {watts:>+6}W  {bar}")

        print()
        print("NOTE: This is a DRY RUN — no commands were sent to the device.")
        print("      To apply the plan, iterate over plan.actions and call")
        print("      await battery.send_command(DeviceCommand(...))")


asyncio.run(main())

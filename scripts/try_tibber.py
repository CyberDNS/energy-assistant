"""
Quick sanity check for the tibber_iobroker plugin against a real ioBroker instance.

Run with:
    python scripts/try_tibber.py

Device configuration is loaded from config.yaml in the project root (gitignored).
Copy config.yaml.example → config.yaml and fill in your values.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from energy_manager.plugins._iobroker.client import IoBrokerClient
from energy_manager.plugins.tibber_iobroker.tariff import TibberIoBrokerTariff

_ROOT = Path(__file__).parent.parent
with open(_ROOT / "config.yaml") as _f:
    _cfg = yaml.safe_load(_f)

HOST = _cfg["iobroker"]["host"]
PORT = int(_cfg["iobroker"]["port"])
HOME_ID = _cfg["tibber_iobroker"]["home_id"]


async def main() -> None:
    print(f"Connecting to ioBroker at {HOST}:{PORT} …\n")

    async with IoBrokerClient(HOST, PORT) as client:
        tariff = TibberIoBrokerTariff(
            tariff_id="tibber",
            client=client,
            home_id=HOME_ID,
            include_tomorrow=True,
        )

        # Current price
        now = datetime.now(timezone.utc)
        price = await tariff.price_at(now)
        print(f"Current price:  {price:.4f} EUR/kWh")
        print()

        # Next 12 hours
        schedule = await tariff.price_schedule(timedelta(hours=12))
        if not schedule:
            print("⚠  No schedule data returned — check that the tibberlink adapter is running.")
            return

        print(f"Price schedule — next {len(schedule)} slots:")
        for pt in schedule:
            local = pt.timestamp.astimezone()
            bar = "█" * int(pt.price_eur_per_kwh * 100)
            print(f"  {local.strftime('%H:%M')}  {pt.price_eur_per_kwh:.4f}  {bar}")


asyncio.run(main())

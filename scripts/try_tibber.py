"""
Quick sanity check for the tibber_iobroker plugin against a real ioBroker instance.

Run with:
    python scripts/try_tibber.py

Credentials are loaded from secrets.yaml in the project root (gitignored).
Copy secrets.yaml.example → secrets.yaml and fill in your values.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from energy_manager.plugins._iobroker.client import IoBrokerClient
from energy_manager.plugins.tibber_iobroker.tariff import TibberIoBrokerTariff
from energy_manager.secrets import SecretsManager

_secrets = SecretsManager(Path(__file__).parent.parent / "secrets.yaml")

HOST = _secrets.get("iobroker_host")
PORT = int(_secrets.get("iobroker_port"))
HOME_ID = _secrets.get("tibber_home_id")


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

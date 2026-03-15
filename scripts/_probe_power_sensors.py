"""One-shot probe: read current values of all candidate home power sensors."""
import asyncio
import httpx

OIDS = [
    "hass.0.entities.sensor.power_production_pv.state",
    "hass.0.entities.sensor.power_production_now.state",
    "hass.0.entities.sensor.power_production_total.state",
    "hass.0.entities.sensor.power_production_overflow_pv.state",
    "hass.0.entities.sensor.power_production_overflow_pv_only_household.state",
    "hass.0.entities.sensor.power_production_household_export.state",
    "hass.0.entities.sensor.power_production_grid_export.state",
    "hass.0.entities.sensor.power_production_grid_bidirectional.state",
    "hass.0.entities.sensor.power_consumption_household.state",
    "hass.0.entities.sensor.power_storage_batteries_bidirectional.state",
    "hass.0.entities.sensor.power_storage_batteries_export.state",
]


async def probe() -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        for oid in OIDS:
            r = await c.get(f"http://192.168.2.30:8087/get/{oid}")
            val = r.json().get("val") if r.status_code == 200 else f"HTTP {r.status_code}"
            name = oid.split("sensor.")[-1].replace(".state", "")
            print(f"  {name:<50s} = {val}")


asyncio.run(probe())

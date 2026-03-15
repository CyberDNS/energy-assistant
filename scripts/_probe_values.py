import asyncio
import httpx

async def main():
    oids = [
        "zendure-solarflow.0.gDa3tb.B1613x21.electricLevel",
        "zendure-solarflow.0.gDa3tb.B1613x21.packPower",
        "zendure-solarflow.0.gDa3tb.B1613x21.packNum",
        "zendure-solarflow.0.gDa3tb.B1613x21.inverseMaxPower",
        "zendure-solarflow.0.gDa3tb.B1613x21.minSoc",
        "zendure-solarflow.0.gDa3tb.B1613x21.socSet",
        "zendure-solarflow.0.gDa3tb.B1613x21.solarInputPower",
        "zendure-solarflow.0.gDa3tb.B1613x21.outputHomePower",
        "zendure-solarflow.0.gDa3tb.B1613x21.outputPackPower",
        "zendure-solarflow.0.gDa3tb.B1613x21.packInputPower",
        "pvforecast.0.plants.pv.power.hoursToday.10:00:00",
        "pvforecast.0.plants.pv.power.hoursToday.12:00:00",
        "pvforecast.0.plants.pv.power.hoursToday.14:00:00",
    ]
    async with httpx.AsyncClient(timeout=10.0) as c:
        for oid in oids:
            r = await c.get("http://192.168.2.30:8087/get/" + oid)
            val = r.json().get("val", "?") if r.status_code == 200 else f"HTTP {r.status_code}"
            print(f"  {oid.split('.')[-1]:30s} = {val}")

asyncio.run(main())

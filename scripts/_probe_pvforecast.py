"""Quick probe of pvforecast.0 JSON OIDs."""
import asyncio
import json
import httpx


async def probe(oid: str) -> None:
    async with httpx.AsyncClient(base_url="http://bear.cyberdns.org:8087", timeout=10) as c:
        r = c.get(f"/get/{oid}")
        r = await r
        print(f"\n=== {oid} ===  status={r.status_code}")
        data = r.json()
        val = data.get("val") if isinstance(data, dict) else data
        if not val:
            print("  (empty/None)")
            return
        parsed = json.loads(val) if isinstance(val, str) else val
        if isinstance(parsed, list):
            print(f"  List of {len(parsed)} rows")
            for row in parsed[:3]:
                print(" ", row)
        elif isinstance(parsed, dict):
            print(f"  Dict keys: {list(parsed.keys())}")
            for k, v in parsed.items():
                if isinstance(v, list):
                    print(f"  {k}: [{len(v)} items]  first={v[0] if v else '—'}")
                else:
                    print(f"  {k}: {v}")
        else:
            print("  type:", type(parsed), " value:", str(parsed)[:200])


async def main() -> None:
    await probe("pvforecast.0.plants.pv.JSONData")
    await probe("pvforecast.0.plants.pv.JSONTable")


asyncio.run(main())

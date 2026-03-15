"""
Probe the ioBroker simple-api to discover available OIDs for a given adapter prefix.

Usage:
    python scripts/probe_iobroker.py <adapter-prefix>

Examples:
    python scripts/probe_iobroker.py zendure
    python scripts/probe_iobroker.py pvforecast
    python scripts/probe_iobroker.py solarforecast
"""

import asyncio
import sys
from pathlib import Path

import httpx

from energy_manager.secrets import SecretsManager

_secrets = SecretsManager(Path(__file__).parent.parent / "secrets.yaml")

HOST = _secrets.get("iobroker_host")
PORT = int(_secrets.get("iobroker_port"))
BASE = f"http://{HOST}:{PORT}"


async def main(prefix: str) -> None:
    print(f"Querying ioBroker at {BASE} for objects matching prefix '{prefix}' …\n")

    # /objects?pattern=<prefix>* returns a dict of all matching objects
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(f"{BASE}/objects", params={"pattern": f"{prefix}*"})
        resp.raise_for_status()
        objects: dict = resp.json()

    if not objects:
        print(f"  (no objects found for prefix '{prefix}')")
        return

    # Group by adapter namespace (first two segments)
    from collections import defaultdict
    groups: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for oid, obj in sorted(objects.items()):
        ns = ".".join(oid.split(".")[:2])
        obj_type = obj.get("type", "?")
        common = obj.get("common", {})
        role = common.get("role", "")
        unit = common.get("unit", "")
        name = common.get("name", "")
        if isinstance(name, dict):
            name = name.get("en", next(iter(name.values()), ""))
        groups[ns].append((oid, obj_type, f"{role}  {unit}  {name}".strip()))

    for ns, items in groups.items():
        print(f"── {ns} ({len(items)} objects) ──")
        for oid, obj_type, meta in items:
            print(f"  [{obj_type:10s}] {oid}")
            if meta.strip():
                print(f"             {meta}")
        print()


prefix = sys.argv[1] if len(sys.argv) > 1 else "zendure"
asyncio.run(main(prefix))

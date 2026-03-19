"""Probe all devices defined in config.yaml and print their current readings.

Usage:
    python scripts/probe_devices.py [--config config.yaml]

Each raw meter fires a single ioBroker read; the differential device is then
computed locally from those readings, exactly as the runtime does it.
A clear PASS / FAIL line is printed for every device so connectivity issues
are immediately obvious.  The estimated cost/earnings for 1 h at the current
power level is also shown, using the tariff configured for each device.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running without 'pip install -e .'
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
import yaml


# ── ioBroker helpers ──────────────────────────────────────────────────────────

async def iobroker_get_val(host: str, port: int, oid: str, timeout: float = 5.0) -> object:
    """Read a single OID and return the raw ``val`` (any type)."""
    url = f"http://{host}:{port}/get/{oid}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            return data.get("val") if isinstance(data, dict) else data
    except Exception as exc:
        return exc


async def iobroker_get(host: str, port: int, oid: str, timeout: float = 5.0) -> object:
    """Read a single OID and cast the value to float (or return the exception)."""
    val = await iobroker_get_val(host, port, oid, timeout)
    if isinstance(val, Exception):
        return val
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError) as exc:
        return exc


def _ok(value: object) -> bool:
    return isinstance(value, (int, float))


def _fmt(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{value:>10.1f} W"
    return f"  ERROR — {value}"


# ── tariff helpers ────────────────────────────────────────────────────────────

def _fmt_cost(power_w: float, import_price: float, export_price: float) -> str:
    """Return a cost string for 1 h at *power_w*.

    Positive cost = paying; negative cost = earning (feed-in).
    """
    if power_w >= 0:
        cost = power_w / 1000.0 * import_price
        return f"{cost:>+8.4f} €/h"
    else:
        cost = power_w / 1000.0 * export_price   # negative * positive = negative
        return f"{cost:>+8.4f} €/h  (feed-in)"


async def _fetch_tibber_price(host: str, port: int, home_id: str) -> float | None:
    """Fetch the current Tibber spot price via CurrentPrice.total OID."""
    oid = f"tibberlink.0.Homes.{home_id}.CurrentPrice.total"
    raw = await iobroker_get(host, port, oid)
    return raw if _ok(raw) else None  # type: ignore[return-value]


async def _build_tariff_prices(
    tariffs_cfg: dict, host: str, port: int
) -> dict[str, dict[str, float]]:
    """Return {tariff_name: {"import": float, "export": float}} for all tariffs.

    For tibber_iobroker tariffs the current price is fetched via CurrentPrice.total.
    """
    result: dict[str, dict[str, float]] = {}

    tibber_tariffs = {
        name: cfg["home_id"]
        for name, cfg in tariffs_cfg.items()
        if cfg.get("type") == "tibber_iobroker"
    }
    tibber_prices: dict[str, float | None] = {}
    if tibber_tariffs:
        fetched = await asyncio.gather(*[
            _fetch_tibber_price(host, port, home_id)
            for home_id in tibber_tariffs.values()
        ])
        tibber_prices = dict(zip(tibber_tariffs.keys(), fetched))

    for name, cfg in tariffs_cfg.items():
        t = cfg.get("type", "")
        if t == "flat_rate":
            result[name] = {
                "import": float(cfg.get("import_price_eur_per_kwh", 0.0)),
                "export": float(cfg.get("export_price_eur_per_kwh", 0.0)),
            }
        elif t == "tibber_iobroker":
            price = tibber_prices.get(name)
            result[name] = {
                "import": price if price is not None else 0.0,
                "export": 0.0,
                "_live": price is not None,
            }

    return result


# ── main ──────────────────────────────────────────────────────────────────────

async def main(config_path: str) -> None:
    cfg = yaml.safe_load(Path(config_path).read_text())

    backends = cfg.get("backends", {})
    iob = backends.get("iobroker", {})
    host = iob.get("host", "localhost")
    port = iob.get("port", 8087)

    tariffs_cfg = cfg.get("tariffs", {})
    tariff_prices = await _build_tariff_prices(tariffs_cfg, host, port)

    # Show live Tibber prices that were fetched
    for name, prices in tariff_prices.items():
        if prices.get("_live"):
            print(f"  Tibber [{name}]: {prices['import']:.4f} €/kWh (current hour)")

    print(f"\nConnecting to ioBroker at {host}:{port}\n")
    W = 118  # total table width
    print(f"{'Device':<22} {'OID / source':<60} {'Value':>10}  {'Cost/h':>12}  Status")
    print("─" * W)

    readings: dict[str, dict[str, float | None]] = {}

    def _cost_str(dev_id: str, power_w: float | None, device_cfg: dict) -> str:
        if power_w is None:
            return f"{'—':>12}"
        tariff_name = device_cfg.get("tariff")
        prices = tariff_prices.get(tariff_name, {}) if tariff_name else {}
        if not prices:
            return f"{'(no tariff)':>12}"
        return _fmt_cost(power_w, prices.get("import", 0.0), prices.get("export", 0.0))

    for device in cfg.get("devices", []):
        dev_id = device["id"]
        dev_type = device.get("type")

        if dev_type == "tibber_iobroker":
            home_id = device.get("home_id", "")
            power_oid = f"tibberlink.0.Homes.{home_id}.LiveMeasurement.power"
            price_oid = f"tibberlink.0.Homes.{home_id}.CurrentPrice.total"

            power_val, price_val = await asyncio.gather(
                iobroker_get(host, port, power_oid),
                iobroker_get(host, port, price_oid),
            )
            power_w = power_val if _ok(power_val) else None
            status = "✓ PASS" if _ok(power_val) else "✗ FAIL"
            price_note = f"  @ {price_val:.4f} €/kWh" if _ok(price_val) else "  (price unavailable)"
            cost = _cost_str(dev_id, power_w, device)
            print(f"{dev_id:<22} {power_oid:<60} {_fmt(power_val)}  {cost}  {status}{price_note}")
            readings[dev_id] = {"power_w": power_w}
            print()

        elif dev_type == "generic_iobroker":
            oid_single = device.get("oid_power")
            oid_import = device.get("oid_power_import")
            oid_export = device.get("oid_power_export")
            if oid_single:
                val = await iobroker_get(host, port, oid_single)
                power_w = val if _ok(val) else None
                status = "✓ PASS" if _ok(val) else "✗ FAIL"
                cost = _cost_str(dev_id, power_w, device)
                print(f"{dev_id:<22} {oid_single:<60} {_fmt(val)}  {cost}  {status}")
                readings[dev_id] = {"power_w": power_w}

            elif oid_import and oid_export:
                imp, exp = await asyncio.gather(
                    iobroker_get(host, port, oid_import),
                    iobroker_get(host, port, oid_export),
                )
                ok = _ok(imp) and _ok(exp)
                net = imp - exp if ok else None  # type: ignore[operator]
                status = "✓ PASS" if ok else "✗ FAIL"
                print(f"{dev_id:<22} {oid_import:<60} {_fmt(imp)}  {'':>12}  {status} (raw import)")
                print(f"{'':22} {oid_export:<60} {_fmt(exp)}  {'':>12}         (raw export)")
                direction = "importing" if (net or 0) >= 0 else "exporting"
                net_label = f"  power_w = import − export  [→ {direction}]"
                cost = _cost_str(dev_id, net, device)
                print(f"{'':22} {net_label:<60} {_fmt(net) if ok else '  —':>10}  {cost}")
                readings[dev_id] = {
                    "import_w": imp if _ok(imp) else None,
                    "export_w": exp if _ok(exp) else None,
                    "power_w": net,
                }

        elif dev_type == "differential":
            minuend_id = device.get("minuend")
            subtrahend_id = device.get("subtrahend")
            min_w = float(device.get("min_w", 0.0))
            max_w_cfg = device.get("max_w")
            max_w = float(max_w_cfg) if max_w_cfg is not None else None

            m_val = readings.get(minuend_id, {}).get("power_w")
            s_val = readings.get(subtrahend_id, {}).get("power_w")

            print(f"{dev_id:<22} {'  ' + minuend_id + ' (power_w)':<60} {_fmt(m_val)}  {'':>12}")
            print(f"{'':22} {'  ' + subtrahend_id + ' (power_w)':<60} {_fmt(s_val)}  {'':>12}")

            if m_val is not None and s_val is not None:
                diff = m_val - s_val
                if max_w is not None:
                    diff = min(diff, max_w)
                diff = max(diff, min_w)
                formula = f"  = max({minuend_id} − {subtrahend_id}, {min_w})"
                cost = _cost_str(dev_id, diff, device)
                print(f"{'':22} {formula:<60} {_fmt(diff)}  {cost}  ✓ PASS (derived)")
                readings[dev_id] = {"power_w": diff}
            else:
                missing = minuend_id if m_val is None else subtrahend_id
                print(f"{'':22} {'  = (unavailable)':<60} {'  —':>10}  {'':>12}  ✗ FAIL ('{missing}' missing)")
                readings[dev_id] = {"power_w": None}

        else:
            print(f"{dev_id:<22} {'type=' + dev_type:<60} {'  —':>10}  {'':>12}  ? SKIP")

        print()  # blank line between devices for readability

    all_pass = all(d.get("power_w") is not None for d in readings.values())
    print("─" * W)
    if all_pass:
        print("All devices reachable. ✓\n")
    else:
        failed = [did for did, d in readings.items() if d.get("power_w") is None]
        print(f"Failed devices: {', '.join(failed)} ✗\n")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe energy_assistant devices.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()
    asyncio.run(main(args.config))

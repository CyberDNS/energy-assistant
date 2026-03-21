"""Manual control test for storage devices (Zendure + SMA Sunny Boy Storage).

Reads the current state, sends a power command, then reads state again so you
can verify the device responded.  Safe to run: it prints everything before
writing and asks for confirmation unless --yes is given.

Usage examples
--------------
# List OIDs that actually exist in ioBroker for a device (diagnose path issues)
python scripts/control_storage.py --device zendure --probe

# Read current state only (no write)
python scripts/control_storage.py --device zendure

# Discharge Zendure at 800 W
python scripts/control_storage.py --device zendure --power -800

# Charge Zendure at 500 W
python scripts/control_storage.py --device zendure --power 500

# Idle Zendure (stop charge AND discharge)
python scripts/control_storage.py --device zendure --power 0

# Discharge SMA battery at 2000 W (negative = producing)
python scripts/control_storage.py --device sma_battery --power -2000

# Block SMA battery discharge
python scripts/control_storage.py --device sma_battery --power 0

# Skip the confirmation prompt
python scripts/control_storage.py --device zendure --power -800 --yes
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running without 'pip install -e .'
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import yaml

from energy_assistant.core.models import DeviceCommand, DeviceState
from energy_assistant.plugins._iobroker.client import IoBrokerClient
from energy_assistant.plugins.sma_modbus_iobroker.device import SmaSunnyBoyStorageDevice
from energy_assistant.plugins.zendure_iobroker.device import ZendureIoBrokerDevice


# ── Config loader ─────────────────────────────────────────────────────────────

def _load_config(path: Path) -> dict:
    if not path.exists():
        print(f"WARNING: {path} not found, using defaults.")
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _find_device_cfg(cfg: dict, device_id: str) -> dict:
    """Return the device config dict for *device_id*, or {} if not found."""
    devices = cfg.get("devices", [])
    if isinstance(devices, list):
        for entry in devices:
            if isinstance(entry, dict) and entry.get("id") == device_id:
                return {k: v for k, v in entry.items() if k != "id"}
    elif isinstance(devices, dict):
        return devices.get(device_id, {})
    return {}


# ── Device factory ────────────────────────────────────────────────────────────

def _make_client(cfg: dict) -> IoBrokerClient:
    iob = (cfg.get("backends") or {}).get("iobroker") or {}
    return IoBrokerClient(
        host=iob.get("host", "127.0.0.1"),
        port=int(iob.get("port", 8087)),
        api_token=iob.get("api_token"),
    )


def _make_zendure(device_id: str, dev_cfg: dict, client: IoBrokerClient) -> ZendureIoBrokerDevice:
    return ZendureIoBrokerDevice(
        device_id=device_id,
        client=client,
        hub_id=dev_cfg.get("hub_id", ""),
        device_serial=dev_cfg.get("device_serial", ""),
        capacity_kwh=float(dev_cfg.get("capacity_kwh", 0.0)),
        max_charge_kw=float(dev_cfg.get("max_charge_kw", 1.2)),
        max_discharge_kw=float(dev_cfg.get("max_discharge_kw", 1.2)),
        maintenance_charge_w=float(dev_cfg.get("maintenance_charge_w", 300.0)),
    )


def _make_sma(device_id: str, dev_cfg: dict, client: IoBrokerClient) -> SmaSunnyBoyStorageDevice:
    return SmaSunnyBoyStorageDevice(
        device_id=device_id,
        client=client,
        modbus_instance=dev_cfg.get("modbus_instance", "modbus.0"),
        capacity_kwh=float(dev_cfg.get("capacity_kwh", 0.0)),
        max_charge_kw=float(dev_cfg.get("max_charge_kw", 3.7)),
        max_discharge_kw=float(dev_cfg.get("max_discharge_kw", 3.7)),
        voltage_max_v=float(dev_cfg.get("voltage_max_v", 253.0)),
        voltage_nominal_v=float(dev_cfg.get("voltage_nominal_v", 230.0)),
    )


# ── Output formatting ─────────────────────────────────────────────────────────

def _fmt_state(state: DeviceState) -> None:
    if not state.available:
        print("  status : UNAVAILABLE")
        return
    pw = state.power_w
    if pw is None:
        pw_str = "  n/a"
    elif pw > 0:
        pw_str = f"{pw:>+8.1f} W  (charging)"
    elif pw < 0:
        pw_str = f"{pw:>+8.1f} W  (discharging)"
    else:
        pw_str = f"{pw:>+8.1f} W  (idle)"
    print(f"  power  : {pw_str}")
    if state.soc_pct is not None:
        print(f"  SoC    : {state.soc_pct:>5.1f} %")
    for key, val in state.extra.items():
        print(f"  {key:<22}: {val}")


def _describe_command(device_type: str, power_w: float) -> str:
    """Human-readable description of what the command will do."""
    if device_type == "zendure_iobroker":
        if power_w > 0:
            return f"charge at {power_w:.0f} W  (sets acMode=1, inputLimit={power_w:.0f} W)"
        elif power_w < 0:
            return f"discharge at {abs(power_w):.0f} W  (sets acMode=2, outputLimit={abs(power_w):.0f} W)"
        else:
            return "idle (sets inputLimit=0, outputLimit=0)"
    else:  # sma_modbus_iobroker
        if power_w >= 0:
            return "block discharge  (sets WirkleistungBeg = 0 %)"
        else:
            return f"discharge at up to {abs(power_w):.0f} W  (sets WirkleistungBeg as %)"


# ── Main logic ────────────────────────────────────────────────────────────────

async def probe_oids(client: IoBrokerClient, pattern: str) -> None:
    """Query ioBroker /objects to list all state OIDs matching *pattern*.

    Helps diagnose wrong OID paths — shows exactly what the adapter exposes.
    """
    import httpx as _httpx
    # The simple-api /objects endpoint accepts a wildcard pattern.
    url = f"/objects"
    try:
        raw_client = client._client  # underlying httpx.AsyncClient
        resp = await raw_client.get(url, params={"pattern": pattern, "type": "state"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  ERROR querying /objects: {exc}")
        return

    if not data:
        print(f"  No OIDs found matching pattern: {pattern}")
        print("  Check that the adapter is running and the hub_id / device_serial are correct.")
        return

    oids = sorted(data.keys()) if isinstance(data, dict) else []
    print(f"  Found {len(oids)} state OIDs matching '{pattern}':")
    for oid in oids:
        obj = data[oid]
        common = obj.get("common", {}) if isinstance(obj, dict) else {}
        write = "rw" if common.get("write") else "r "
        unit  = common.get("unit", "")
        name  = common.get("name", "")
        print(f"    [{write}]  {oid}  {unit}  {name}")


async def main(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    cfg = _load_config(config_path)
    client = _make_client(cfg)
    dev_cfg = _find_device_cfg(cfg, args.device)

    # Determine device type and instantiate
    dev_type = dev_cfg.get("type", "")
    if dev_type == "zendure_iobroker" or (not dev_type and "hub_id" in dev_cfg):
        device = _make_zendure(args.device, dev_cfg, client)
        label = "Zendure SolarFlow"
        supports_charge = True
    elif dev_type == "sma_modbus_iobroker" or (not dev_type and "modbus_instance" in dev_cfg):
        device = _make_sma(args.device, dev_cfg, client)
        label = "SMA Sunny Boy Storage"
        supports_charge = False
    else:
        print(
            f"ERROR: device '{args.device}' not found in {config_path} or type unrecognised."
            "\nMake sure the device is listed under 'devices:' with type:"
            " 'zendure_iobroker' or 'sma_modbus_iobroker'."
        )
        sys.exit(1)

    # ── Probe mode ────────────────────────────────────────────────────────────
    if args.probe:
        prefix = (
            f"zendure-solarflow.0.{dev_cfg.get('hub_id', '*')}.{dev_cfg.get('device_serial', '*')}.*"
            if dev_type == "zendure_iobroker"
            else f"{dev_cfg.get('modbus_instance', 'modbus.0')}.*"
        )
        print(f"\n{'─'*55}")
        print(f"  Device : {args.device}  ({label})")
        print(f"  Probing ioBroker OIDs: {prefix}")
        print(f"{'─'*55}")
        await probe_oids(client, prefix)
        return

    # ── Read current state ────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"  Device : {args.device}  ({label})")
    print(f"{'─'*55}")
    print("  Reading current state …")
    state_before = await device.get_state()
    _fmt_state(state_before)

    # ── No command → done ─────────────────────────────────────────────────────
    if args.power is None:
        print(f"\n  (No --power given — read-only.)")
        return

    power_w = float(args.power)

    # Warn about unsupported charge command for SMA
    if not supports_charge and power_w > 0:
        print(
            "\n  WARNING: SMA Sunny Boy Storage does not support explicit charge commands."
            "\n           Charging is managed automatically by the inverter."
            "\n           Use --power 0 to block discharge, or a negative value to allow it."
        )
        sys.exit(1)

    # ── Confirm before writing ────────────────────────────────────────────────
    action = _describe_command(dev_type, power_w)
    print(f"\n  Command: set_power_w({power_w:+.0f} W)")
    print(f"  Action : {action}")

    if not args.yes:
        try:
            answer = input("\n  Send this command? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return
        if answer != "y":
            print("  Aborted.")
            return

    # ── Send command ──────────────────────────────────────────────────────────
    print("\n  Sending command …")
    try:
        await device.send_command(DeviceCommand(
            device_id=args.device,
            command="set_power_w",
            value=power_w,
        ))
        print("  Command sent OK.")
    except Exception as exc:
        print(f"  ERROR sending command: {exc}")
        sys.exit(1)

    # ── Read state again ──────────────────────────────────────────────────────
    print(f"\n  Waiting {args.wait} s for device to respond …")
    await asyncio.sleep(args.wait)

    print("  Reading state after command …")
    state_after = await device.get_state()
    _fmt_state(state_after)

    print(f"{'─'*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read state and optionally send a set_power_w command to a storage device.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--device",
        required=True,
        metavar="ID",
        help="Device ID as it appears in config.yaml (e.g. 'zendure' or 'sma_battery').",
    )
    parser.add_argument(
        "--power",
        type=float,
        default=None,
        metavar="W",
        help=(
            "Target power in W with sign: negative = discharge, positive = charge"
            " (charge only for Zendure), 0 = idle / block discharge."
            " Omit to read state without writing."
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: config.yaml in cwd).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=5.0,
        metavar="S",
        help="Seconds to wait between writing and re-reading state (default: 5).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help=(
            "Query ioBroker for all OIDs under the device prefix and print them."
            " Use this to diagnose wrong OID paths (no writes are performed)."
        ),
    )
    asyncio.run(main(parser.parse_args()))

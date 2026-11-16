"""Probe a Modbus TCP device with a profile and print what it reports.

Usage:
    python scripts/probe_modbus.py --host 192.168.1.50
    python scripts/probe_modbus.py --host 192.168.1.50 --profile sma_sunny_boy_storage --unit-id 3
    python scripts/probe_modbus.py --list

Read-only: nothing is ever written to the device.

Every register the profile declares is read on its own, so one unsupported
address does not hide the others.  For each register the raw words, the
decoded value and the profile's "no value" marker are shown — compare them
with the vendor's register list (or the values in ioBroker) to verify a
profile.  Finally the resulting ``DeviceState`` is printed, exactly as the
runtime would see it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running without 'pip install -e .'
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from energy_assistant.plugins._modbus import codec  # noqa: E402
from energy_assistant.plugins._modbus.client import ModbusError, ModbusTcpClient  # noqa: E402
from energy_assistant.plugins.modbus.device import ModbusDevice  # noqa: E402
from energy_assistant.plugins.modbus.profile import (  # noqa: E402
    ProfileError,
    available_profiles,
    load_profile,
)


async def probe(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"ERROR: {exc}")
        return 2

    port = args.port or profile.port
    unit_id = args.unit_id if args.unit_id is not None else profile.unit_id
    print(f"Profile : {profile.name}")
    print(f"Target  : {args.host}:{port}  unit id {unit_id}\n")

    client = ModbusTcpClient(args.host, port, timeout=args.timeout)
    polled = {r.name for r in profile.polled_registers()}
    failures = 0
    try:
        print(f"{'register':<22}{'table':<9}{'addr':>6}  {'type':<5}{'raw':<22}value")
        for reg in sorted(profile.registers.values(), key=lambda r: (r.table, r.address)):
            head = f"{reg.name:<22}{reg.table:<9}{reg.address:>6}  {reg.type:<5}"
            try:
                words = await client.read(reg.table, reg.address, reg.count, unit_id)
            except ModbusError as exc:
                note = "" if reg.name in polled else "  (not polled by the runtime)"
                failures += reg.name in polled
                print(f"{head}{'-':<22}FAILED: {exc}{note}")
                continue
            raw = " ".join(f"{w:04X}" for w in words)
            sentinel = profile.nan.get(reg.type)
            if sentinel is not None and codec.raw_unsigned(words, profile.word_order) == sentinel:
                value = "no value (NaN marker)"
            else:
                value = str(codec.decode(words, reg.type, profile.word_order) * reg.scale)
            print(f"{head}{raw:<22}{value}")

        state = await ModbusDevice("probe", client, profile, unit_id=unit_id).get_state()
        print("\nDeviceState as the runtime sees it:")
        print(f"  available : {state.available}")
        print(f"  power_w   : {state.power_w}   (positive = consuming / charging)")
        print(f"  soc_pct   : {state.soc_pct}")
        print(f"  energy_kwh: {state.energy_kwh}")
        for key, value in state.extra.items():
            print(f"  extra.{key}: {value}")
    finally:
        await client.close()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", help="IP address or hostname of the device")
    parser.add_argument("--profile", default="sma_sunny_boy_storage",
                        help="bundled profile name or path to a profile .yaml")
    parser.add_argument("--port", type=int, help="TCP port (default: from profile)")
    parser.add_argument("--unit-id", type=int, help="Modbus unit id (default: from profile)")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--list", action="store_true", help="list bundled profiles and exit")
    args = parser.parse_args()

    if args.list:
        print("\n".join(available_profiles()))
        return 0
    if not args.host:
        parser.error("--host is required")
    return asyncio.run(probe(args))


if __name__ == "__main__":
    raise SystemExit(main())

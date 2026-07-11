"""Live test for the openwb_mqtt plugin — step by step, confirmed manually.

Drives one chargepoint through the real plugin code path
(``OpenWBMqttDevice.send_command`` → simpleAPI set topics):

  1. Stop
  2. PV charging
  3. Instant charging @ 6 A, SoC limit 70 %
  4. Instant charging @ 10 A
  5. Instant charging @ 16 A
  6. SoC limit → 80 %
  7. PV charging
  8. Stop
  9. Restore the original chargemode and SoC limit

Each step first announces what it is about to set and waits for Enter.
After executing it prints the read-back topics (polling until openWB's
read topics catch up); verify in the openWB UI, then Enter continues to
the next step.  NOTE: the instant steps will really start charging when
a car is plugged in and below the SoC limit.

Usage (from the repo root):

    python scripts/openwb_live_test.py                     # openwb_chargepoint_4
    python scripts/openwb_live_test.py openwb_chargepoint_5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from energy_assistant.config.yaml import YamlConfigLoader  # noqa: E402
from energy_assistant.core.models import DeviceCommand  # noqa: E402
from energy_assistant.core.plugin_registry import BuildContext  # noqa: E402
from energy_assistant.plugins import registry  # noqa: E402
from energy_assistant.plugins.openwb_mqtt.device import _normalize_mode  # noqa: E402

PHASES = 3
VOLTAGE = 230.0
READBACK_TIMEOUT_S = 10.0


def _watts(amps: int) -> float:
    return amps * PHASES * VOLTAGE


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _readback(device) -> str:
    b, cp = device._bridge, device._cp_id
    return (
        f"chargemode={b.value(cp, 'chargemode')}  "
        f"power={b.value(cp, 'power')} W  "
        f"charge_state={b.value(cp, 'charge_state')}  "
        f"limit={b.value(cp, 'instant_charging_limit')}/"
        f"{b.value(cp, 'instant_charging_limit_soc')}%  "
        f"soc={b.value(cp, 'soc/soc')}%"
    )


async def _confirm(prompt: str) -> None:
    # input() must not block the event loop — the MQTT listener task keeps
    # the read-back cache fresh while we wait.
    await asyncio.to_thread(input, prompt)


async def step(label: str, device, make_coro, expect_mode: str | None = None) -> None:
    """Announce the operation, wait for Enter, execute, print the read-back.

    openWB's read topics lag its internal control cycle by a few seconds,
    so when *expect_mode* is given we keep polling until the chargemode
    read topic reflects it (up to READBACK_TIMEOUT_S).
    """
    print(f"\n── NEXT: {label}")
    await _confirm("   Enter to execute…")
    print(f"[{_now()}]    sent.")
    await make_coro()
    if expect_mode is not None:
        deadline = asyncio.get_event_loop().time() + READBACK_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            current = _normalize_mode(device._bridge.value(device._cp_id, "chargemode"))
            if current == expect_mode:
                break
            await asyncio.sleep(1.0)
    else:
        await asyncio.sleep(2.0)  # give openWB a moment to reflect the change
    print(f"[{_now()}]    readback: {_readback(device)}")
    print("   Check the openWB UI now.")


def _cmd(device_id: str, value: float) -> DeviceCommand:
    return DeviceCommand(device_id=device_id, command="set_power_w", value=value)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device_id", nargs="?", default="openwb_chargepoint_4")
    args = parser.parse_args()

    cfg = YamlConfigLoader("config.yaml").load()
    dcfg = cfg.devices.get(args.device_id)
    if dcfg is None or dcfg.get("type") != "openwb_mqtt":
        sys.exit(f"{args.device_id!r} is not an openwb_mqtt device in config.yaml")

    device = registry.build_device(args.device_id, dcfg, BuildContext(backends=cfg.backends))
    bridge, cp = device._bridge, device._cp_id

    # Connect and let retained messages fill the cache.
    await device.get_state()
    await asyncio.sleep(3)
    if not bridge.connected:
        sys.exit("MQTT bridge did not connect")

    original_mode = _normalize_mode(bridge.value(cp, "chargemode"))
    original_limit = bridge.value(cp, "instant_charging_limit_soc")
    print(f"[{_now()}] chargepoint {cp}: initial {_readback(device)}")
    print(f"[{_now()}] will restore mode={original_mode!r} limit={original_limit}% at the end")

    def send(value: float):
        return lambda: device.send_command(_cmd(device.device_id, value))

    def instant(amps: int, soc_limit: float):
        async def run() -> None:
            device.update_target_soc(soc_limit)
            await device.send_command(_cmd(device.device_id, _watts(amps)))
        return run

    try:
        await step("set chargemode → stop", device, send(0.0), expect_mode="stop")
        await step("set chargemode → pv", device, send(1.0), expect_mode="pv")
        await step(
            f"set chargemode → instant, 6 A ({_watts(6):.0f} W), SoC limit 70 %",
            device, instant(6, 70.0), expect_mode="instant",
        )
        await step(
            f"set chargecurrent → 10 A ({_watts(10):.0f} W), instant unchanged",
            device, instant(10, 70.0),
        )
        await step(
            f"set chargecurrent → 16 A ({_watts(16):.0f} W), instant unchanged",
            device, instant(16, 70.0),
        )
        await step(
            "set SoC limit → 80 % (still instant @ 16 A)",
            device, instant(16, 80.0),
        )
        await step("set chargemode → pv", device, send(1.0), expect_mode="pv")
        await step("set chargemode → stop", device, send(0.0), expect_mode="stop")

    except (KeyboardInterrupt, EOFError):
        print("\naborted — restoring original state")
    finally:
        print(f"\n── FINAL: restore mode={original_mode!r}, SoC limit {original_limit}%")
        if original_mode is not None:
            await bridge.publish_set(cp, "chargemode", original_mode)
        if original_limit not in (None, "null"):
            await bridge.publish_set(cp, "instant_charging_limit_soc", original_limit)
        deadline = asyncio.get_event_loop().time() + READBACK_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:  # read-topic lag
            if _normalize_mode(bridge.value(cp, "chargemode")) == original_mode:
                break
            await asyncio.sleep(1.0)
        print(f"[{_now()}]    readback: {_readback(device)}")
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())

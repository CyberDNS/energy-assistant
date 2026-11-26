"""Read-only probe for the native ``zendure`` plugin.

Logs in to the Zendure cloud with the authorization cloud key, lists the
devices on the account (product/device keys for config.yaml), then connects
to the cloud MQTT broker and prints the merged state for a while.  Nothing is
written to the device — only a ``getAll`` read request is published.

Usage::

    # key from config.yaml (device 'zendure', cloud_key: !secret ...)
    python scripts/probe_zendure.py --config config.yaml --device zendure
    # or directly
    ZENDURE_CLOUD_KEY=... python scripts/probe_zendure.py --seconds 60

Note: the cloud hands out one MQTT client id per account.  While the ioBroker
adapter (or nornwatt) is connected with the same key, the broker may
kick one of the two connections.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import yaml

from nornwatt.plugins.zendure import cloud_bridge
from nornwatt.plugins.zendure.cloud import cloud_login
from nornwatt.plugins.zendure.device import ZendureDevice


def _cloud_key_from_config(path: Path, device_id: str) -> tuple[str | None, dict]:
    from nornwatt.config.yaml import _make_loader
    from nornwatt.secrets import SecretsManager

    loader = _make_loader(SecretsManager(path.parent / "secrets.yaml"))
    with path.open() as f:
        cfg = yaml.load(f, Loader=loader) or {}  # noqa: S506
    for entry in cfg.get("devices") or []:
        if isinstance(entry, dict) and entry.get("id") == device_id:
            return entry.get("cloud_key"), entry
    return None, {}


async def main(args: argparse.Namespace) -> None:
    dev_cfg: dict = {}
    key = os.environ.get("ZENDURE_CLOUD_KEY")
    if not key and args.config:
        key, dev_cfg = _cloud_key_from_config(Path(args.config), args.device)
    if not key:
        sys.exit("No cloud key: set ZENDURE_CLOUD_KEY or point --config/--device at a "
                 "'zendure' device with cloud_key.")

    login = await cloud_login(key)
    print(f"MQTT broker: {login.mqtt.host}:{login.mqtt.port}")
    print("Devices on this account:")
    for d in login.devices:
        print(f"  {d.name or '?':<20} model={d.model:<14} product_key={d.product_key} "
              f"device_key={d.device_key} online={d.online} ip={d.ip}")
    if not login.devices:
        return

    pk = str(dev_cfg.get("product_key") or dev_cfg.get("hub_id") or login.devices[0].product_key)
    dk = str(dev_cfg.get("device_key") or dev_cfg.get("device_serial") or login.devices[0].device_key)
    bridge = cloud_bridge(key)
    device = ZendureDevice("probe", bridge, pk, dk, capacity_kwh=0.0,
                           max_charge_kw=0.0, max_discharge_kw=0.0)

    print(f"\nWatching {pk}/{dk} for {args.seconds}s …")
    for _ in range(max(1, args.seconds // 5)):
        await asyncio.sleep(5)
        state = await device.get_state()
        if not state.available:
            print("  (no report yet)")
            continue
        print(f"  power={state.power_w:+.0f} W  soc={state.soc_pct} %  {state.extra}")
    await bridge.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("NORNWATT_CONFIG"))
    p.add_argument("--device", default="zendure")
    p.add_argument("--seconds", type=int, default=30)
    asyncio.run(main(p.parse_args()))

"""zendure plugin — Zendure SolarFlow / Hyper battery via MQTT, no ioBroker.

Two connection modes:

``connection: cloud`` (default)
    Logs in to the Zendure cloud with the *authorization cloud key* from the
    Zendure app (Profile → Developer / Home Assistant integration key) and
    connects to the Zendure cloud MQTT broker — the same path the ioBroker
    adapter's ``authKey`` mode uses.  Works for every Zendure model,
    including the Hyper 2000 (which has no local HTTP / zenSDK API).

``connection: local``
    Uses the ``backends.mqtt`` broker.  The device must already publish to
    that broker (redirected local MQTT); topics are identical.

Example config::

    - id: zendure
      category: battery
      type: zendure
      cloud_key: !secret zendure_cloud_key
      product_key: "aBc1dE"      # (also accepted: hub_id)
      device_key: "AB12cd34"     # (also accepted: device_serial)
      capacity_kwh: 7.6
      max_charge_kw: 1.2
      max_discharge_kw: 0.8
      # smart_mode: true         # keep limit writes in RAM (no flash wear)
      # poll_interval_s: 60      # full-report request interval
      # release_auto_model: 8    # own mode on release (default: the mode the
      #                          # device had before Energy Assistant took over)

``python scripts/probe_zendure.py`` lists the keys of all devices on the
account and dumps live reports (read-only).
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.config import MqttConfig
from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry, SharedPool
from .bridge import BrokerParams, ZendureBridge
from .cloud import cloud_login, decode_cloud_key
from .device import ZendureDevice

_log = logging.getLogger(__name__)

# One bridge per broker connection — the cloud hands out a single client id
# per account, so devices on the same account must share the connection.
_bridges: SharedPool[tuple[Any, ...], ZendureBridge] = SharedPool(
    stop=lambda bridge: bridge.stop()
)


SETTINGS = {
    "product_key": ParamSpec(str, description="Product key of the device"),
    "device_key": ParamSpec(str, description="Device key (serial)"),
    "hub_id": ParamSpec(str, legacy=True, description="Old name of product_key"),
    "device_serial": ParamSpec(str, legacy=True, description="Old name of device_key"),
    "connection": ParamSpec(str, choices=("cloud", "local"), default="cloud",
                            description="Zendure cloud broker or the local MQTT backend"),
    "cloud_key": ParamSpec(str, secret=True, required=True, when={"connection": "cloud"},
                           description="Authorization cloud key from the Zendure app (cloud mode)"),
    "maintenance_charge_w": ParamSpec(float, minimum=0, default=300.0,
                                      label="Maintenance charge power (W)",
                                      description="Charge power while the device asks for a "
                                                  "maintenance charge (W)"),
    "smart_mode": ParamSpec(bool, advanced=True, default=False, description="Enable the device's smartMode on start-up, so limit "
                                                 "writes do not wear its flash"),
    "release_auto_model": ParamSpec(int, advanced=True, minimum=0, label="Own mode (autoModel)",
                                    description="Mode the device returns to when Energy "
                                                "Assistant lets go of it (Zendure 'autoModel', "
                                                "e.g. 8 = smart matching); default: the mode it "
                                                "had before Energy Assistant took over"),
    "poll_interval_s": ParamSpec(float, advanced=True, minimum=0, exclusive_minimum=True, default=60.0,
                                 description="Seconds between full reads"),
    "stale_after_s": ParamSpec(float, advanced=True, minimum=0, exclusive_minimum=True, default=300.0,
                               description="Seconds without a report until the device counts as "
                                           "unavailable"),
}


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "zendure", _build,
        title="Zendure battery",
        description="SolarFlow / Hyper battery over the Zendure cloud broker or the local MQTT broker.",
        categories={C.BATTERY},
        provides={Cap.POWER_W, Cap.SOC_PCT, Cap.POWER_SETPOINT},
        settings=SETTINGS,
    )


def cloud_bridge(cloud_key: str, *, poll_interval_s: float = 60.0) -> ZendureBridge:
    async def params() -> BrokerParams:
        login = await cloud_login(cloud_key)
        m = login.mqtt
        return BrokerParams(
            host=m.host, port=m.port, username=m.username, password=m.password,
            client_id=m.client_id, mqtt_v5=True,
        )

    return _bridges.get(
        ("cloud", cloud_key),
        lambda: ZendureBridge("cloud", params, poll_interval_s=poll_interval_s),
    )


def local_bridge(cfg: MqttConfig, *, poll_interval_s: float = 60.0) -> ZendureBridge:
    async def params() -> BrokerParams:
        return BrokerParams(
            host=cfg.host, port=cfg.port, username=cfg.username, password=cfg.password,
        )

    return _bridges.get(
        ("local", cfg.host, cfg.port),
        lambda: ZendureBridge(f"{cfg.host}:{cfg.port}", params, poll_interval_s=poll_interval_s),
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> ZendureDevice | None:
    product_key = str(cfg.get("product_key") or cfg.get("hub_id") or "")
    device_key = str(cfg.get("device_key") or cfg.get("device_serial") or "")
    if not product_key or not device_key:
        _log.warning(
            "Device %r (zendure): 'product_key' and 'device_key' required — skipping", device_id
        )
        return None

    poll_interval_s = float(cfg.get("poll_interval_s", 60.0))
    connection = str(cfg.get("connection", "cloud")).lower()
    if connection == "cloud":
        cloud_key = cfg.get("cloud_key")
        if not cloud_key:
            _log.warning("Device %r (zendure): 'cloud_key' required for cloud mode — skipping",
                         device_id)
            return None
        decode_cloud_key(str(cloud_key))  # fail fast on a malformed key
        bridge = cloud_bridge(str(cloud_key), poll_interval_s=poll_interval_s)
    elif connection == "local":
        if ctx.backends.mqtt is None:
            _log.warning("Device %r (zendure): local mode requires backends.mqtt — skipping",
                         device_id)
            return None
        bridge = local_bridge(ctx.backends.mqtt, poll_interval_s=poll_interval_s)
    else:
        raise ValueError(
            f"zendure device {device_id!r}: connection must be 'cloud' or 'local', "
            f"got {connection!r}"
        )

    release_auto_model = cfg.get("release_auto_model")
    purchase_price = cfg.get("purchase_price_eur")
    cycle_life = cfg.get("cycle_life")
    return ZendureDevice(
        device_id=device_id,
        bridge=bridge,
        product_key=product_key,
        device_key=device_key,
        capacity_kwh=float(cfg.get("capacity_kwh", 0.0)),
        min_soc_pct=float(cfg.get("min_soc_pct", 0.0)),
        max_soc_pct=float(cfg.get("max_soc_pct", 100.0)),
        max_charge_kw=float(cfg.get("max_charge_kw", 0.0)),
        max_discharge_kw=float(cfg.get("max_discharge_kw", 0.0)),
        maintenance_charge_w=float(cfg.get("maintenance_charge_w", 300.0)),
        purchase_price_eur=float(purchase_price) if purchase_price is not None else None,
        cycle_life=int(cycle_life) if cycle_life is not None else None,
        smart_mode=bool(cfg.get("smart_mode", False)),
        stale_after_s=float(cfg.get("stale_after_s", 300.0)),
        release_auto_model=int(release_auto_model) if release_auto_model is not None else None,
        state_path=(ctx.data_dir / "zendure" / f"{device_id}.json") if ctx.data_dir else None,
    )

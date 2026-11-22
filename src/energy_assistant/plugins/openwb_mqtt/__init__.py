"""openWB EV charger plugin — direct MQTT simpleAPI control (no Home Assistant).

Registers the ``openwb_mqtt`` device type which:
- Reads SoC, charging power, plug state, and charge mode from the openWB
  broker's ``{root}/simpleAPI/chargepoint/{id}/…`` topics.
- Writes chargemode / chargecurrent / instant SoC limit via the
  ``{root}/simpleAPI/set/chargepoint/{id}/…`` topics.

Uses the ``backends.mqtt`` broker connection.  All chargepoints sharing the
same broker and root topic share one MQTT connection.

Example config::

    - id: openwb_chargepoint_5
      category: charger
      type: openwb_mqtt
      chargepoint_id: 5
      root_topic: openwbmaster   # default: openWB

The mode-encoding convention is documented in ``assets/ev.py``.
"""

from __future__ import annotations

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import ParamSpec
from ...core.plugin_registry import PluginRegistry
from .device import OpenWBMqttDevice, get_bridge


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "openwb_mqtt", _build,
        title="openWB (MQTT)",
        description="openWB chargepoint over its MQTT API (needs the MQTT connection).",
        categories={C.CHARGER},
        provides={Cap.POWER_W, Cap.PLUGGED, Cap.POWER_SETPOINT},
        settings={
            "chargepoint_id": ParamSpec(int, required=True, minimum=0, label="Chargepoint number",
                                        description="openWB chargepoint number"),
            "root_topic": ParamSpec(str, default="openWB", label="MQTT root topic", description="MQTT root topic of openWB"),
            "phases": ParamSpec(int, minimum=1, maximum=3, default=3,
                                description="Phases the chargepoint charges with"),
            "voltage_v": ParamSpec(float, advanced=True, minimum=0, exclusive_minimum=True, default=230.0),
            "min_current_a": ParamSpec(float, advanced=True, minimum=0, default=6),
            "max_current_a": ParamSpec(float, advanced=True, minimum=0, exclusive_minimum=True, default=16),
            "mode_instant": ParamSpec(str, advanced=True, default="instant", description="openWB mode name"),
            "mode_pv": ParamSpec(str, advanced=True, default="pv", description="openWB mode name"),
            "mode_stop": ParamSpec(str, advanced=True, default="stop", description="openWB mode name"),
        },
    )


def _build(device_id: str, cfg: dict, ctx) -> OpenWBMqttDevice:  # type: ignore[type-arg]
    mqtt_cfg = ctx.backends.mqtt
    if mqtt_cfg is None:
        raise RuntimeError(
            f"openwb_mqtt device '{device_id}' requires an MQTT backend "
            "(backends.mqtt in config.yaml)"
        )
    if "chargepoint_id" not in cfg:
        raise ValueError(f"openwb_mqtt device '{device_id}' requires 'chargepoint_id'")

    bridge = get_bridge(mqtt_cfg, cfg.get("root_topic", "openWB"))
    return OpenWBMqttDevice(
        device_id=device_id,
        bridge=bridge,
        chargepoint_id=cfg["chargepoint_id"],
        phases=cfg.get("phases", 3),
        voltage_v=cfg.get("voltage_v", 230.0),
        min_current_a=cfg.get("min_current_a", 6),
        max_current_a=cfg.get("max_current_a", 16),
        mode_instant=cfg.get("mode_instant", "instant"),
        mode_pv=cfg.get("mode_pv", "pv"),
        mode_stop=cfg.get("mode_stop", "stop"),
    )

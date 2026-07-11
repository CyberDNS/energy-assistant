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
      role: ev_charger
      type: openwb_mqtt
      chargepoint_id: 5
      root_topic: openwbmaster   # default: openWB

The mode-encoding convention is documented in ``assets/ev.py``.
"""

from __future__ import annotations

from ...core.plugin_registry import PluginRegistry
from .device import OpenWBMqttDevice, get_bridge


def register(registry: PluginRegistry) -> None:
    registry.register_device("openwb_mqtt", _build)


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

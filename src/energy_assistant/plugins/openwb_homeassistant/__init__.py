"""openWB EV charger plugin for Home Assistant.

Registers the ``openwb_homeassistant`` device type which:
- Reads SoC, charging power, and plug/cable status from HA sensors.
- Writes the charging mode (PV Charging / Instant Charging / Stop) via
  a HA ``select`` entity.

The mode-encoding convention is documented in ``assets/ev.py``.
"""

from __future__ import annotations

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import ParamSpec
from ...core.plugin_registry import PluginRegistry
from .device import OpenWBDevice


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "openwb_homeassistant", _build,
        title="openWB (via Home Assistant)",
        categories={C.CHARGER},
        provides={Cap.POWER_W, Cap.PLUGGED, Cap.POWER_SETPOINT},
        settings={
            "entity_mode": ParamSpec(str, required=True, entity=True, label="Charge mode entity",
                                     description="Charge mode select entity"),
            "entity_soc": ParamSpec(str, required=True, entity=True, label="SoC entity",
                                    description="SoC sensor of the chargepoint"),
            "entity_power": ParamSpec(str, entity=True, label="Power entity", description="Charging power sensor"),
            "entity_plugged": ParamSpec(str, entity=True, label="Plug state entity", description="Plug state sensor"),
            "entity_soc_limit_instant": ParamSpec(str, entity=True, advanced=True,
                                                  label="Instant SoC limit entity",
                                                  description="Instant charging SoC limit entity"),
            "entity_current_instant": ParamSpec(str, entity=True, advanced=True,
                                                label="Instant current entity",
                                                description="Instant charging current entity"),
            "phases": ParamSpec(int, minimum=1, maximum=3, default=3,
                                description="Phases the chargepoint charges with"),
            "voltage_v": ParamSpec(float, advanced=True, minimum=0, exclusive_minimum=True, default=230.0),
            "mode_pv": ParamSpec(str, advanced=True, default="PV Charging", description="Option text for PV mode"),
            "mode_instant": ParamSpec(str, advanced=True, default="Instant Charging",
                                      description="Option text for instant mode"),
            "mode_stop": ParamSpec(str, advanced=True, default="Stop", description="Option text for stop"),
        },
    )


def _build(device_id: str, cfg: dict, ctx) -> OpenWBDevice:  # type: ignore[type-arg]
    ha_client = ctx.ha_client
    if ha_client is None:
        raise RuntimeError(
            f"openwb_homeassistant device '{device_id}' requires a "
            "Home Assistant backend (backends.homeassistant in config.yaml)"
        )
    return OpenWBDevice(
        device_id=device_id,
        client=ha_client,
        entity_mode=cfg["entity_mode"],
        entity_soc=cfg["entity_soc"],
        entity_power=cfg.get("entity_power"),
        entity_plugged=cfg.get("entity_plugged"),
        entity_soc_limit_instant=cfg.get("entity_soc_limit_instant"),
        entity_current_instant=cfg.get("entity_current_instant"),
        phases=cfg.get("phases", 3),
        voltage_v=cfg.get("voltage_v", 230.0),
        mode_pv=cfg.get("mode_pv", "PV Charging"),
        mode_instant=cfg.get("mode_instant", "Instant Charging"),
        mode_stop=cfg.get("mode_stop", "Stop"),
    )

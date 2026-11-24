"""openwb_homeassistant — openWB through Home Assistant (old configuration form).

Kept for existing configs: the flat ``entity_*`` settings are translated into
a ``homeassistant_charger`` with mode control (``homeassistant_charger``):

  entity_mode               → power_setpoint.mode
  mode_instant / _pv / _stop → modes charge / absorb_surplus / stop
  entity_current_instant    → modes.charge.current
  entity_soc_limit_instant  → power_setpoint.soc_limit
  entity_power, entity_plugged → capabilities power_w, plugged (without a plug
                              sensor a car is taken as always connected)
  phases, voltage_v         → the same settings

``entity_soc`` is still accepted but no longer read: a car's SoC comes from its
vehicle device.  New configs use ``type: homeassistant_charger``.
"""

from __future__ import annotations

from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry

TYPE = "openwb_homeassistant"


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        TYPE, _build,
        title="openWB (via Home Assistant, old form)",
        description="Use 'Charger from Home Assistant' with charge modes for new devices.",
        legacy=True,
        categories={C.CHARGER},
        provides={Cap.POWER_W, Cap.PLUGGED, Cap.POWER_SETPOINT},
        settings={
            "entity_mode": ParamSpec(str, required=True, entity=True, label="Charge mode entity",
                                     description="Charge mode select entity"),
            "entity_soc": ParamSpec(str, entity=True, legacy=True, label="SoC entity",
                                    description="No longer read (the vehicle has the SoC)"),
            "entity_power": ParamSpec(str, entity=True, label="Power entity",
                                      description="Charging power sensor"),
            "entity_plugged": ParamSpec(str, entity=True, label="Plug state entity",
                                        description="Plug state sensor"),
            "entity_soc_limit_instant": ParamSpec(str, entity=True, advanced=True,
                                                  label="Instant SoC limit entity",
                                                  description="Instant charging SoC limit entity"),
            "entity_current_instant": ParamSpec(str, entity=True, advanced=True,
                                                label="Instant current entity",
                                                description="Instant charging current entity"),
            "phases": ParamSpec(int, minimum=1, maximum=3, default=3,
                                description="Phases the chargepoint charges with"),
            "voltage_v": ParamSpec(float, advanced=True, minimum=0, exclusive_minimum=True,
                                   default=230.0),
            "mode_pv": ParamSpec(str, advanced=True, default="PV Charging",
                                 description="Option text for PV mode"),
            "mode_instant": ParamSpec(str, advanced=True, default="Instant Charging",
                                      description="Option text for instant mode"),
            "mode_stop": ParamSpec(str, advanced=True, default="Stop",
                                   description="Option text for stop"),
        },
    )


def translate(cfg: dict[str, Any]) -> dict[str, Any]:
    """The ``homeassistant_charger`` config of an old ``openwb_homeassistant`` one."""
    charge: dict[str, Any] = {"option": cfg.get("mode_instant", "Instant Charging")}
    if cfg.get("entity_current_instant"):
        charge["current"] = cfg["entity_current_instant"]
    setpoint: dict[str, Any] = {
        "control": "modes",
        "mode": cfg["entity_mode"],
        "modes": {
            "charge": charge,
            "absorb_surplus": {"option": cfg.get("mode_pv", "PV Charging")},
            "stop": {"option": cfg.get("mode_stop", "Stop")},
        },
    }
    if cfg.get("entity_soc_limit_instant"):
        setpoint["soc_limit"] = cfg["entity_soc_limit_instant"]
    caps: dict[str, Any] = {"power_setpoint": setpoint}
    if cfg.get("entity_power"):
        caps["power_w"] = {"entity": cfg["entity_power"]}
    if cfg.get("entity_plugged"):
        caps["plugged"] = {"entity": cfg["entity_plugged"]}
    return {
        "type": "homeassistant_charger",
        "category": cfg.get("category", "charger"),
        "phases": cfg.get("phases", 3),
        "voltage_v": cfg.get("voltage_v", 230.0),
        "capabilities": caps,
    }


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from ..homeassistant_charger import build

    if ctx.ha_client is None:
        raise RuntimeError(
            f"openwb_homeassistant device '{device_id}' requires a "
            "Home Assistant backend (backends.homeassistant in config.yaml)"
        )
    return build(device_id, translate(cfg), ctx, type_name=TYPE, require_power=False)

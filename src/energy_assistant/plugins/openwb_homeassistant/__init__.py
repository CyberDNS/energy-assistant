"""openWB EV charger plugin for Home Assistant.

Registers the ``openwb_homeassistant`` device type which:
- Reads SoC, charging power, and plug/cable status from HA sensors.
- Writes the charging mode (PV Charging / Instant Charging / Stop) via
  a HA ``select`` entity.

The mode-encoding convention is documented in ``assets/ev.py``.
"""

from __future__ import annotations

from ...core.plugin_registry import PluginRegistry
from .device import OpenWBDevice


def register(registry: PluginRegistry) -> None:
    registry.register_device("openwb_homeassistant", _build)


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
        mode_pv=cfg.get("mode_pv", "PV Charging"),
        mode_instant=cfg.get("mode_instant", "Instant Charging"),
        mode_stop=cfg.get("mode_stop", "Stop"),
    )

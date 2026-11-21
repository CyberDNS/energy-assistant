"""homeassistant_vehicle plugin — an electric car read from Home Assistant.

The car's own data comes from its integration in Home Assistant (VW group,
Nissan, …), bound to capabilities::

    - id: bandit
      category: vehicle
      type: homeassistant_vehicle
      label: Bandit
      capabilities:
        soc_pct:       { entity: sensor.bandit_state_of_charge_car }
        range_km:      { entity: sensor.large_garage_bandit_range }      # optional
        plugged:       { entity: binary_sensor.large_garage_bandit_plugged_in }  # optional
        location_home: { entity: device_tracker.bandit }                 # optional
      refresh_entity: button.large_garage_bandit_refresh_data  # optional
      poll_interval_s: 30            # optional, default 30
      params:
        capacity_kwh: 77
        charge_limit_soc_pct: 90
        charger_ids: { openwb_mqtt: 1 }
        push_soc_to_charger: true

Car integrations update every 15–60 min and not at all while the car sleeps,
so the reading carries the time the integration last reported it
(``extra["soc_timestamp"]``); the session logic estimates the SoC in between
from the charger's energy (``core/vehicles.py``).  The car's own plug sensor
is informational only (``extra["car_plugged"]``): the charger's plug state
starts and ends a charging session.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import DeviceConfigError
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)

_BINDABLE = {Cap.SOC_PCT, Cap.RANGE_KM, Cap.PLUGGED, Cap.LOCATION_HOME}


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "homeassistant_vehicle", _build,
        categories={C.VEHICLE},
        bindable=_BINDABLE,
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import HAVehicleDevice

    caps = cfg.get("capabilities") or {}
    entities: dict[Cap, str] = {}
    for name, binding in caps.items():
        entity = binding.get("entity")
        unknown = set(binding) - {"entity"}
        if not isinstance(entity, str) or not entity or unknown:
            raise DeviceConfigError([(
                f"device {device_id!r} (homeassistant_vehicle): capabilities.{name} must be "
                "{entity: <entity id>}"
            )])
        entities[Cap(name)] = entity
    if ctx.ha_client is None:
        _log.warning(
            "Device %r (homeassistant_vehicle) requires Home Assistant backend — skipping",
            device_id,
        )
        return None
    refresh = cfg.get("refresh_entity")
    return HAVehicleDevice(
        device_id=device_id,
        client=ctx.ha_client,
        entities=entities,
        refresh_entity=str(refresh) if refresh else None,
        poll_interval_s=float(cfg.get("poll_interval_s", 30.0)),
    )

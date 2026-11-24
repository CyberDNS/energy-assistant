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
from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry
from .._homeassistant.bindings import bindings_for, parse_bindings

_log = logging.getLogger(__name__)

_BINDABLE = {Cap.SOC_PCT, Cap.RANGE_KM, Cap.PLUGGED, Cap.LOCATION_HOME}


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "homeassistant_vehicle", _build,
        title="Car from Home Assistant",
        description="SoC, range, plug and location from the car integration in Home Assistant.",
        categories={C.VEHICLE},
        bindable=_BINDABLE,
        bindings=bindings_for(_BINDABLE),
        settings={
            "refresh_entity": ParamSpec(str, entity=True, label="Refresh button",
                                        description="Button entity that makes the car report "
                                                    "fresh values"),
            "poll_interval_s": ParamSpec(float, advanced=True, minimum=0, exclusive_minimum=True, default=30.0,
                                         description="Seconds between reads"),
        },
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import HAVehicleDevice

    readings = parse_bindings(device_id, "homeassistant_vehicle", cfg, _BINDABLE)
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
        bindings=readings,
        refresh_entity=str(refresh) if refresh else None,
        poll_interval_s=float(cfg.get("poll_interval_s", 30.0)),
    )

"""homeassistant_switch plugin — an on/off switch (plug, relay) in Home Assistant.

Device type: ``homeassistant_switch``   (category ``switch``)

Configuration
-------------
``capabilities.switch.entity``   — HA entity switched on/off (``switch.*``,
                                   ``input_boolean.*``, ``light.*``, ``fan.*``).
``capabilities.power_w``         — optional measured power of the plug (any
                                   power binding, see ``_homeassistant/bindings``).
``capabilities.energy_in_kwh``   — optional energy counter of the plug.
``params.rated_power_w``         — power the load draws while on (W).  Used
                                   as the power reading when no power sensor
                                   is bound, and by the planner for the device
                                   this switch controls.
``release_state``                — what the switch does when Nornwatt
                                   lets go of it (stop, reload, stale plan,
                                   stuck control loop): ``on`` (default, the
                                   load's own thermostat takes over), ``off``
                                   or ``leave`` (unchanged).

Example::

    - id: aquarium_cooler_plug
      category: switch
      type: homeassistant_switch
      capabilities:
        switch: { entity: switch.aquarium_cooler_plug }
      params: { rated_power_w: 150 }

A switch executes the plan of the device that is ``controlled_by`` it (a
``thermal_load``); that device sends its commands through the switch.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory as C
from ...core.device_model import DeviceConfigError, ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry
from .._homeassistant.actuators import SWITCH_BINDING, parse_switch
from .._homeassistant.bindings import binding_specs, parse_bindings

_log = logging.getLogger(__name__)

_READINGS = frozenset({Cap.POWER_W, Cap.ENERGY_IN_KWH})
RELEASE_STATES = ("on", "off", "leave")


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "homeassistant_switch", _build,
        title="Home Assistant switch",
        categories={C.SWITCH},
        bindable={Cap.SWITCH, *_READINGS},
        bindings={
            Cap.SWITCH: SWITCH_BINDING,
            **{cap: binding_specs(cap) for cap in _READINGS},
        },
        settings={
            "release_state": ParamSpec(
                str, choices=RELEASE_STATES, default="on", label="When released",
                choice_labels={"on": "Switch on", "off": "Switch off", "leave": "Leave as is"},
                description="State when Nornwatt stops or lets go of the switch "
                            "(on: the load's own thermostat or timer takes over)"),
        },
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import HASwitchDevice

    switch_binding = (cfg.get("capabilities") or {}).get("switch")
    if not switch_binding:
        raise DeviceConfigError([(
            f"device {device_id!r} (homeassistant_switch) needs "
            "'capabilities: {switch: {entity: …}}'"
        )])
    readings = parse_bindings(device_id, "homeassistant_switch", cfg, _READINGS)
    if ctx.ha_client is None:
        _log.warning(
            "Device %r (homeassistant_switch) requires Home Assistant backend — skipping",
            device_id,
        )
        return None
    switch = parse_switch(f"device {device_id!r}", ctx.ha_client, switch_binding)
    rated = cfg.get("rated_power_w")
    return HASwitchDevice(
        device_id=device_id,
        client=ctx.ha_client,
        switch=switch,
        power=readings.get(Cap.POWER_W),
        energy=readings.get(Cap.ENERGY_IN_KWH),
        rated_power_w=float(rated) if rated is not None else None,
        release_state=str(cfg.get("release_state") or "on"),
    )

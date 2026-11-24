"""energy_assistant plugin registry — auto-wires all built-in plugins.

Import this module to get a fully populated ``PluginRegistry``::

    from energy_assistant.plugins import registry

The singleton is populated once at import time; each built-in plugin
calls ``register(registry)`` to add its factory functions.

Adding a new plugin
-------------------
1. Create ``plugins/my_plugin/__init__.py`` with a ``register(registry)`` function.
2. Add an import and a ``my_plugin.register(registry)`` call below.
"""

from __future__ import annotations

from ..core.plugin_registry import PluginRegistry

registry = PluginRegistry()

# Import and register each built-in plugin.
# Order matters for readability only; dependency ordering (deferred=True)
# is handled inside the registry, not here.
from . import (  # noqa: E402
    differential,
    flat_rate,
    forecast_solar,
    generic_consumer,
    homeassistant_battery,
    homeassistant_charger,
    homeassistant_sensor,
    homeassistant_switch,
    homeassistant_thermal,
    homeassistant_thermostat,
    homeassistant_vehicle,
    learned,
    modbus,
    openwb_homeassistant,
    openwb_mqtt,
    static_profile,
    tasmota,
    tibber,
    tibber_local,
    time_of_use,
    zendure,
)

differential.register(registry)
flat_rate.register(registry)
forecast_solar.register(registry)
generic_consumer.register(registry)
homeassistant_battery.register(registry)
homeassistant_charger.register(registry)
homeassistant_sensor.register(registry)  # also the old name generic_homeassistant
homeassistant_switch.register(registry)
homeassistant_thermal.register(registry)
homeassistant_thermostat.register(registry)
homeassistant_vehicle.register(registry)
learned.register(registry)
modbus.register(registry)
openwb_homeassistant.register(registry)
openwb_mqtt.register(registry)
static_profile.register(registry)
tasmota.register(registry)
tibber.register(registry)
tibber_local.register(registry)
time_of_use.register(registry)
zendure.register(registry)

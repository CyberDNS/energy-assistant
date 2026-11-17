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
    generic_homeassistant,
    generic_iobroker,
    learned,
    modbus,
    openwb_homeassistant,
    openwb_mqtt,
    pvforecast_iobroker,
    sma_modbus_iobroker,
    static_profile,
    threshold_homeassistant,
    tibber,
    tibber_iobroker,
    tibber_local,
    zendure,
    zendure_iobroker,
)

differential.register(registry)
flat_rate.register(registry)
forecast_solar.register(registry)
generic_consumer.register(registry)
generic_homeassistant.register(registry)
generic_iobroker.register(registry)
learned.register(registry)
modbus.register(registry)
openwb_homeassistant.register(registry)
openwb_mqtt.register(registry)
pvforecast_iobroker.register(registry)
sma_modbus_iobroker.register(registry)
static_profile.register(registry)
threshold_homeassistant.register(registry)
tibber.register(registry)
tibber_iobroker.register(registry)
tibber_local.register(registry)
zendure.register(registry)
zendure_iobroker.register(registry)

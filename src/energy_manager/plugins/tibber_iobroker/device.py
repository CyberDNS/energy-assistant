"""
Tibber via ioBroker (tibberlink adapter) — device plugin.

Reads real-time home energy data (consumption, production) that the
tibberlink adapter exposes as ioBroker state objects.

Tibberlink object paths (examples)
------------------------------------
- ``tibberlink.0.Homes.<HOME_ID>.realtime.powerProduction``  — W from solar
- ``tibberlink.0.Homes.<HOME_ID>.realtime.powerConsumption``  — W consumed
- ``tibberlink.0.Homes.<HOME_ID>.realtime.accumulatedConsumption``  — kWh today

Use ``IoBrokerDevice`` from ``energy_manager.plugins._iobroker.device`` and
map the object IDs you need via ``state_map``::

    from energy_manager.plugins._iobroker.device import IoBrokerDevice
    from energy_manager.plugins._iobroker.pool import IoBrokerConnectionPool
    from energy_manager.core.models import DeviceCategory

    pool = IoBrokerConnectionPool()
    HOME = "aa115263-6d29-4e80-8190-fb95ddd4e743"

    grid_meter = IoBrokerDevice(
        device_id="tibber_home",
        category=DeviceCategory.METER,
        client=pool.get("192.168.2.30"),
        state_map={
            "power_w":    f"tibberlink.0.Homes.{HOME}.realtime.powerConsumption",
            "energy_kwh": f"tibberlink.0.Homes.{HOME}.realtime.accumulatedConsumption",
        },
    )

This module is intentionally left as documentation/example only.  Import
``IoBrokerDevice`` directly from ``energy_manager.plugins._iobroker.device``
— there is no Tibber-specific device class needed beyond the generic one.
"""

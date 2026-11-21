"""modbus plugin — devices read directly over Modbus TCP, described by a profile.

Config::

    devices:
      sma_battery:
        category: battery
        type: modbus
        profile: sma_sunny_boy_storage   # bundled name, or path to a .yaml file
        host: 192.168.1.50
        # port: 502        # optional, default from the profile
        # unit_id: 3       # optional, default from the profile
        # timeout_s: 3
        # read_only: true  # optional: never write, even if the profile can
        max_discharge_kw: 3.7   # needed by the SMA control strategy
        # ...plus the usual storage keys (capacity_kwh, min_soc_pct, …)

No ioBroker or Home Assistant backend is involved.  Whether and how the
device can be commanded is defined by the profile's ``control`` section.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory
from ...core.device_model import DeviceCategory as C
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device(
        "modbus", _build,
        categories={C.BATTERY, C.PV, C.GRID_METER, C.METER, C.CONSUMER},
        provides={Cap.POWER_W, Cap.SOC_PCT},
    )


def _build(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .._modbus.client import ModbusConnectionPool
    from .control import ControlConfigError, build_strategy
    from .device import ModbusDevice
    from .profile import ProfileError, load_profile

    host = cfg.get("host")
    profile_ref = cfg.get("profile")
    if not host or not profile_ref:
        _log.warning("Device %r (modbus) requires 'host' and 'profile' — skipping", device_id)
        return None
    try:
        profile = load_profile(profile_ref)
    except ProfileError as exc:
        _log.warning("Device %r (modbus): %s — skipping", device_id, exc)
        return None

    if ctx.modbus_pool is None:
        ctx.modbus_pool = ModbusConnectionPool()
    client = ctx.modbus_pool.get(
        host=str(host),
        port=int(cfg.get("port", profile.port)),
        timeout=float(cfg.get("timeout_s", 3.0)),
    )
    strategy = None
    if profile.control is not None and not cfg.get("read_only", False):
        try:
            strategy = build_strategy(profile.control.strategy, profile.control.registers, cfg)
        except ControlConfigError as exc:
            _log.warning(
                "Device %r (modbus): control disabled, device is read-only — %s", device_id, exc
            )

    unit_id = cfg.get("unit_id")
    return ModbusDevice(
        device_id=device_id,
        client=client,
        profile=profile,
        category=DeviceCategory(cfg["category"]),
        unit_id=int(unit_id) if unit_id is not None else None,
        strategy=strategy,
    )

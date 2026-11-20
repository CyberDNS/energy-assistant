"""tasmota plugin — read Tasmota devices directly over the LAN.

Polls ``http://<host>/cm?cmnd=Status 10`` (no cloud, no MQTT broker) and maps
the sensor JSON to a device reading.  Works for

- IR reading heads on smart meters (Hichi, bitShake, …) running an SML/OBIS
  script → ``role: meter``;
- smart plugs / relays with energy monitoring (the ``ENERGY`` sensor) →
  ``role: consumer``;
- any other Tasmota sensor that reports power in its JSON.

The value names come from the device — for meters from the meter script —
so they are mapped to readings by a **profile** (bundled in ``profiles/``, or
your own YAML file, see ``profile.py``) and/or by ``fields``: dotted paths
below ``StatusSNS``.  Look at ``http://<host>/cm?cmnd=Status%2010`` to see
them.  If the power value isn't found, the log names the bundled profiles that
match the payload and the power-like values in it.

Example config::

    devices:
      # Main grid meter (Z1) via a Tasmota IR head running the MT175 docs script
      - id: main_grid_meter
        role: meter
        type: tasmota
        tariff: grid
        host: 192.168.3.46               # IP or hostname
        profile: iskra_mt175             # bundled name, or path to a .yaml file
        # password: !secret tasmota_password   # web password, if set (user: admin)
        poll_interval_s: 2               # default: 2 for meters, 10 otherwise
        # stale_after_s: 30              # unavailable after this long without a valid reading

      # Same meter, mapped by hand (no profile)
      - id: main_grid_meter
        role: meter
        type: tasmota
        host: 192.168.3.46
        fields:
          power_w: MT631.P               # net, import +, export −
          energy_in_kwh: MT631.E_in
          energy_out_kwh: MT631.E_out
          phase_power_w: [MT631.L1, MT631.L2, MT631.L3]
        # invert_sign: true              # if the script reports export as positive

      # Smart plug with standard energy monitoring
      - id: dishwasher
        role: consumer
        type: tasmota
        host: 192.168.3.60
        profile: energy                  # ENERGY.Power, ENERGY.Total, Voltage, Current

Profiles, sensor and fields
---------------------------
A profile names a sensor object (``MT175``, ``ENERGY``) and paths relative to
it.  If the device calls its sensor differently (the meter name in the
script's ``+1,…`` line is often changed), the one sensor object holding the
power value is used; set ``sensor:`` to pick it explicitly.  ``fields`` given
next to a profile override the profile's, field by field (relative to the
sensor too); ``null`` removes one.  Without a profile, ``fields`` paths start
at ``StatusSNS`` unless ``sensor:`` is set.

Fields: ``power_w`` (required), ``energy_in_kwh``, ``energy_out_kwh``,
``voltage_v``, ``current_a``: a path, or ``{path: …, scale: …}`` for scripts
reporting kW or Wh (``scale: 1000`` turns kW into W, ``0.001`` Wh into kWh).
Quote paths that look like numbers (``"1_8_0"``).  A list element is
addressed by index (``Power.1``); a list value of ``power_w`` or the counters
(multi-channel plugs) is summed.  ``phase_power_w``, ``phase_voltage_v``,
``phase_current_a``: lists of paths for L1..L3.  ``extra``: further
``name: path`` values copied into ``DeviceState.extra``.

Readings are stamped with the UTC receive time; the device's ``Time`` is local
time without an offset and is ignored.  Only HTTP polling is implemented; the
``TelePeriod`` of MQTT ``tele/…/SENSOR`` (10 s by default) is too slow for
control.  Switching relays (``Power On/Off``) is not supported yet.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import DeviceRole, parse_device_role
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device("tasmota", _build_device)


def _number(cfg: dict[str, Any], key: str, default: float, device_id: str) -> float:
    raw = cfg.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _log.warning("Device %r (tasmota): invalid %s %r — using %s", device_id, key, raw, default)
        return default
    if value <= 0:
        _log.warning("Device %r (tasmota): %s must be > 0 — using %s", device_id, key, default)
        return default
    return value


def _build_device(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .device import TasmotaDevice
    from .mapping import MappingError, build_field_map
    from .poller import TasmotaPoller
    from .profile import ProfileError, load_profile

    host = str(cfg.get("host") or "").strip()
    if not host:
        _log.warning("Device %r (tasmota): 'host' is required — skipping", device_id)
        return None
    try:
        profile = load_profile(cfg["profile"]) if cfg.get("profile") else None
        sensor = cfg.get("sensor") or (profile.sensor if profile else None)
        fields = build_field_map(
            cfg.get("fields"),
            base=profile.fields if profile else None,
            sensor=sensor,
            # The profile's sensor name may differ on the device; an explicit one must match.
            auto_sensor=profile is not None and not cfg.get("sensor"),
            invert_sign=bool(cfg.get("invert_sign", profile.invert_sign if profile else False)),
        )
    except (ProfileError, MappingError) as exc:
        _log.warning("Device %r (tasmota): %s — skipping", device_id, exc)
        return None

    role = parse_device_role(cfg.get("role"), default=DeviceRole.CONSUMER)
    poll_default = 2.0 if role == DeviceRole.METER else 10.0
    poll_interval_s = _number(cfg, "poll_interval_s", poll_default, device_id)
    poller = TasmotaPoller(
        host,
        fields,
        user=str(cfg.get("user") or "admin"),
        password=str(cfg.get("password") or "").strip() or None,
        poll_interval_s=poll_interval_s,
        stale_after_s=_number(cfg, "stale_after_s", max(30.0, 3 * poll_interval_s), device_id),
    )
    return TasmotaDevice(device_id=device_id, role=role, poller=poller)

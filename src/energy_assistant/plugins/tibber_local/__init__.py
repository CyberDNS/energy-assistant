"""tibber_local plugin — live meter data straight from the Tibber Pulse Bridge.

Reads the raw meter telegram from the bridge's built-in web server on the LAN
instead of the Tibber cloud websocket, so a Tibber outage or an expired token
doesn't take the meter down.  Decodes SML 1.04 (binary) and plaintext OBIS
telegrams.

The meter pushes a telegram every 2-3 s and the bridge serves the latest one.
Polling every second (default) picks each one up within a second; a repeated
telegram is ignored and doesn't count as a fresh reading.

Bridge setup
------------
1. Find the bridge on the LAN (its hostname/IP from your router).  The password
   is the code printed on the bridge (QR-code label); the user is ``admin``.
2. Check that ``http://<bridge>/nodes.json`` asks for a login.  If the web
   server is not reachable, enable it permanently: put the bridge in AP mode
   (press the button until it lights up, join its Wi-Fi, open
   ``http://10.133.70.1``), then in the console tab run ``param_set 39 true``
   (``webserver_force_enable``; check with ``param_get 39``) and reboot it.
3. ``node_id`` is the Pulse node shown in ``nodes.json`` (usually 1).

Telegrams are read from ``/node_data.json?node_id=<n>`` (current firmware) or
``/data.json?node_id=<n>`` (older firmware).  If both return 404 the plugin
logs an error and retries every 5 minutes: Tibber may have removed local
access in a firmware update.

Example config::

    devices:
      - id: household_meter
        role: meter
        type: tibber_local
        tariff: household
        host: 192.168.3.18             # or tibber-bridge.lan
        password: !secret tibber_pulse_password
        node_id: 1                     # optional, default 1
        poll_interval_s: 1             # optional, default 1
        stale_after_s: 30              # optional: unavailable after this long without a valid telegram
        # Optional fallback to the Tibber cloud live stream while the bridge has no data:
        token: !secret tibber_token
        home_id: "<home-id>"           # optional

Readings
--------
``power_w``      net active power, OBIS 16.7.0 (import +, export -); meters
                 without 16.7.0 use 1.7.0 - 2.7.0
``energy_kwh``   import counter, OBIS 1.8.0
``extra``        ``meter_production_kwh`` (2.8.0), ``l1..l3_power_w`` /
                 ``_voltage_v`` / ``_current_a``, ``frequency_hz``,
                 ``pulse_battery_v``, ``source`` (``local`` / ``cloud``)
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import parse_device_role
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_device("tibber_local", _build_device)


def _number(cfg: dict[str, Any], key: str, default: float, device_id: str) -> float:
    raw = cfg.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _log.warning("Device %r (tibber_local): invalid %s %r — using %s", device_id, key, raw, default)
        return default
    if value <= 0:
        _log.warning("Device %r (tibber_local): %s must be > 0 — using %s", device_id, key, default)
        return default
    return value


def _build_device(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .bridge import PulseBridgePoller
    from .device import TibberLocalDevice

    host = str(cfg.get("host") or "").strip()
    password = str(cfg.get("password") or "").strip()
    if not host or not password:
        _log.warning("Device %r (tibber_local): 'host' and 'password' are required — skipping", device_id)
        return None

    role = parse_device_role(cfg.get("role"), default=parse_device_role("meter"))
    poller = PulseBridgePoller(
        host,
        password,
        user=str(cfg.get("user") or "admin"),
        node_id=int(_number(cfg, "node_id", 1, device_id)),
        poll_interval_s=_number(cfg, "poll_interval_s", 1.0, device_id),
        stale_after_s=_number(cfg, "stale_after_s", 30.0, device_id),
    )

    fallback = None
    token = str(cfg.get("token") or "").strip()
    if token:
        from ..tibber.api import get_api
        from ..tibber.device import TibberDevice

        fallback = TibberDevice(
            device_id=device_id,
            role=role,
            stream=get_api(token).live_stream(cfg.get("home_id") or None),
        )

    return TibberLocalDevice(device_id=device_id, role=role, poller=poller, fallback=fallback)

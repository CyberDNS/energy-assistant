"""tibber plugin — Tibber spot-price tariff and live meter, directly from the Tibber API.

No ioBroker or Home Assistant needed: prices come from the Tibber GraphQL API,
live power from the Pulse/Watty websocket.  Create a personal access token at
https://developer.tibber.com/settings/access-token.

Example config::

    tariffs:
      household:
        type: tibber
        token: !secret tibber_token
        home_id: "<home-id>"        # optional — defaults to the account's first home
        resolution: QUARTER_HOURLY  # or HOURLY
        default: true

    devices:
      - id: household_meter
        role: meter
        type: tibber
        tariff: household
        token: !secret tibber_token
        home_id: "<home-id>"        # optional
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import parse_device_role
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)

_RESOLUTIONS = ("QUARTER_HOURLY", "HOURLY")


def register(registry: PluginRegistry) -> None:
    registry.register_tariff("tibber", _build_tariff)
    registry.register_device("tibber", _build_device)


def _token(kind: str, item_id: str, cfg: dict[str, Any]) -> str | None:
    token = str(cfg.get("token") or "").strip()
    if not token:
        _log.warning("%s %r (tibber): 'token' is required — skipping", kind, item_id)
        return None
    return token


def _build_tariff(tariff_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .api import get_api
    from .tariff import TibberTariff

    token = _token("Tariff", tariff_id, cfg)
    if token is None:
        return None
    resolution = str(cfg.get("resolution", "QUARTER_HOURLY")).upper()
    if resolution not in _RESOLUTIONS:
        _log.warning(
            "Tariff %r (tibber): unknown resolution %r — using QUARTER_HOURLY", tariff_id, resolution
        )
        resolution = "QUARTER_HOURLY"
    return TibberTariff(
        tariff_id=tariff_id,
        api=get_api(token, resolution),
        home_id=cfg.get("home_id") or None,
    )


def _build_device(device_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .api import get_api
    from .device import TibberDevice

    token = _token("Device", device_id, cfg)
    if token is None:
        return None
    return TibberDevice(
        device_id=device_id,
        role=parse_device_role(cfg.get("role"), default=parse_device_role("meter")),
        stream=get_api(token).live_stream(cfg.get("home_id") or None),
    )

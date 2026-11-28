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
        # api_url: https://api.tibber.com/v1-beta/gql  # optional, e.g. a local replay server for tests
        default: true

    devices:
      - id: household_meter
        category: meter
        type: tibber_cloud
        tariff: household
        token: !secret tibber_token
        home_id: "<home-id>"        # optional
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import Capability as Cap
from ...core.device_model import DeviceCategory, ParamSpec
from ...core.device_model import DeviceCategory as C
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)

_RESOLUTIONS = ("QUARTER_HOURLY", "HOURLY")


_ACCOUNT = {
    "token": ParamSpec(str, required=True, secret=True, label="Tibber API token",
                       description="Personal access token of your Tibber account, from "
                                   "developer.tibber.com"),
    "home_id": ParamSpec(str, label="Tibber home ID",
                         description="Only for an account with several homes: the ID of the "
                                     "home, from developer.tibber.com. Left empty, the first "
                                     "home is used"),
    "api_url": ParamSpec(str, advanced=True, label="Tibber API URL", description="Tests only"),
}


def register(registry: PluginRegistry) -> None:
    registry.register_tariff(
        "tibber", _build_tariff,
        title="Tibber",
        description="Tibber spot prices from the Tibber API.",
        settings={**_ACCOUNT, "resolution": ParamSpec(
            str, choices=_RESOLUTIONS, default="QUARTER_HOURLY", label="Price resolution",
            choice_labels={"QUARTER_HOURLY": "Quarter-hourly", "HOURLY": "Hourly"},
            description="Whether the prices are fetched per quarter hour or per hour")},
    )
    registry.register_device(
        "tibber_cloud", _build_device,
        title="Tibber Pulse (cloud)",
        description="Live meter power from the Tibber cloud (Pulse or Watty).",
        categories={C.METER},
        provides={Cap.POWER_W},
        settings=_ACCOUNT,
    )


def _token(kind: str, item_id: str, cfg: dict[str, Any]) -> str | None:
    token = str(cfg.get("token") or "").strip()
    if not token:
        _log.warning("%s %r (tibber): 'token' is required — skipping", kind, item_id)
        return None
    return token


def _api_url(cfg: dict[str, Any]) -> str | None:
    return str(cfg.get("api_url") or "").strip() or None


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
        api=get_api(token, resolution, _api_url(cfg)),
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
        category=DeviceCategory(cfg["category"]),
        stream=get_api(token, api_url=_api_url(cfg)).live_stream(cfg.get("home_id") or None),
    )

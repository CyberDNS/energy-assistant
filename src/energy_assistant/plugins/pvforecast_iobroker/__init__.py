"""pvforecast_iobroker plugin — PV generation forecast from ioBroker pvforecast adapter."""

from __future__ import annotations

import logging
from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    registry.register_forecast("pvforecast_iobroker", _build)


def _build(forecast_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .forecast import PvForecastIoBrokerForecast

    if ctx.iobroker_pool is None or ctx.backends.iobroker is None:
        _log.warning(
            "Forecast %r (pvforecast_iobroker) requires ioBroker backend — skipping",
            forecast_id,
        )
        return None

    oid = cfg.get("oid", "pvforecast.0.plants.pv.JSONData")
    iob = ctx.backends.iobroker
    client = ctx.iobroker_pool.get(host=iob.host, port=iob.port, api_token=iob.api_token)
    return PvForecastIoBrokerForecast(forecast_id=forecast_id, client=client, oid=oid)

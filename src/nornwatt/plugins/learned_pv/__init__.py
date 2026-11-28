"""learned_pv plugin — PV forecast learned from the measured production.

Instead of describing the installation (orientation, tilt, peak power,
shading), the model learns it from Home Assistant's history of the PV power
sensor, compared hour by hour with Open-Meteo's archived weather forecasts.
The forecast then applies the learned model to Open-Meteo's current
irradiance forecast (up to 16 days ahead) and corrects the next hours by the
deviation measured in the last hour.  See ``core/pv_model.py``.

Example config::

    forecasts:
      pv:
        type: learned_pv
        latitude: 52.52
        longitude: 13.44
        history_entity: sensor.power_production_pv   # PV power sensor (W or kW)
        device: pv_production       # optional: the PV device, for the UI
        # history_days: 730         # history used for training
        # refit_interval_h: 24
        # forecast_days: 7          # 1 … 16
        # capacity_kw: 7.4          # optional: inverter limit (default: highest hourly mean seen)
        # intraday_correction: true
        # fallback: { type: forecast_solar, ... }   # used until a model is fitted

Weather data comes from https://open-meteo.com (free for non-commercial use,
no account).  Each request sends the configured location.  Needs the Home
Assistant backend for the history.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


_LATITUDE = "Latitude of the PV system in degrees, e.g. 49.61. North is positive"
_LONGITUDE = "Longitude of the PV system in degrees, e.g. 6.13. East is positive"


def register(registry: PluginRegistry) -> None:
    registry.register_forecast(
        "learned_pv", _build,
        title="Learned from history (PV)",
        description="PV forecast learned from the measured production and Open-Meteo weather forecasts.",
        scopes=("system",),
        settings={
            "latitude": ParamSpec(float, required=True, minimum=-90, maximum=90,
                                  description=_LATITUDE),
            "longitude": ParamSpec(float, required=True, minimum=-180, maximum=180,
                                   description=_LONGITUDE),
            "history_entity": ParamSpec(str, required=True, entity=True, label="PV power sensor",
                                        description="Sensor of the PV power in Home Assistant. "
                                                    "Its history is what the forecast is "
                                                    "learned from"),
            "device": ParamSpec(str, label="PV device", advanced=True,
                                description="ID of the PV device in Nornwatt. The learned "
                                            "model is then shown on the page of that device"),
            "history_days": ParamSpec(int, minimum=14, default=730, advanced=True,
                                      description="Days of history the forecast is learned "
                                                  "from"),
            "min_days": ParamSpec(int, minimum=1, default=14, advanced=True,
                                  label="Days needed",
                                  description="Days with production needed before the learned "
                                              "forecast is used"),
            "refit_interval_h": ParamSpec(float, minimum=1, default=24, advanced=True,
                                          description="Hours between two refits, when the "
                                                      "forecast is learned again from the "
                                                      "history"),
            "forecast_days": ParamSpec(int, minimum=1, maximum=16, default=7, advanced=True,
                                       label="Weather forecast (days)",
                                       description="How many days of weather forecast are "
                                                   "fetched"),
            "capacity_kw": ParamSpec(float, minimum=0, exclusive_minimum=True, advanced=True,
                                     label="Inverter limit (kW)",
                                     description="The forecast never goes above this. Left "
                                                 "empty: the highest hourly average ever "
                                                 "measured"),
            "intraday_correction": ParamSpec(bool, default=True, advanced=True,
                                             description="When the last hour produced more or "
                                                         "less than forecast, the next hours "
                                                         "are corrected in the same direction"),
            "fallback": ParamSpec(dict, plugin="forecast", label="Fallback forecast",
                                  description="Forecast used until enough history is there to "
                                              "learn from"),
            "forecast_url": ParamSpec(str, advanced=True, description="Tests only"),
            "archive_url": ParamSpec(str, advanced=True, description="Tests only"),
        },
    )


def _num(value: Any) -> float:
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    return float(value)


def _build(forecast_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .. import registry as plugin_registry
    from .forecast import LearnedPvForecast
    from .open_meteo import OpenMeteoClient

    fallback = None
    fallback_cfg = cfg.get("fallback")
    if isinstance(fallback_cfg, dict):
        fallback = plugin_registry.build_forecast(f"{forecast_id}_fallback", fallback_cfg, ctx)

    try:
        latitude, longitude = _num(cfg["latitude"]), _num(cfg["longitude"])
        history_entity = str(cfg["history_entity"])
    except (KeyError, TypeError, ValueError):
        _log.warning("Forecast %r (learned_pv): 'latitude', 'longitude' and 'history_entity' "
                     "are required — skipping", forecast_id)
        return fallback
    if ctx.ha_client is None:
        _log.warning("Forecast %r (learned_pv) needs the homeassistant backend — using %s",
                     forecast_id, "its fallback" if fallback is not None else "no forecast")
        return fallback

    model_path = None
    if ctx.data_dir is not None:
        model_path = ctx.data_dir / "learned_models" / f"{forecast_id}.json"

    weather = OpenMeteoClient(
        latitude, longitude,
        forecast_url=str(cfg.get("forecast_url") or "").strip() or None,
        archive_url=str(cfg.get("archive_url") or "").strip() or None,
    )
    capacity = cfg.get("capacity_kw")
    return LearnedPvForecast(
        forecast_id, ctx.ha_client, weather,
        latitude=latitude, longitude=longitude, history_entity=history_entity,
        history_days=int(cfg.get("history_days", 730)),
        refit_interval_h=_num(cfg.get("refit_interval_h", 24)),
        min_days=int(cfg.get("min_days", 14)),
        capacity_kw=_num(capacity) if capacity not in (None, "") else None,
        forecast_days=int(cfg.get("forecast_days", 7)),
        intraday_correction=bool(cfg.get("intraday_correction", True)),
        device_id=str(cfg["device"]) if cfg.get("device") else None,
        fallback=fallback,
        model_path=model_path,
    )

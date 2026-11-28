"""forecast_solar plugin — PV generation forecast directly from Forecast.Solar.

No ioBroker needed: the forecast comes from https://forecast.solar, the same
service the ioBroker pvforecast adapter uses.  The public API needs no account
(12 requests per hour per IP, one plane per request, hourly resolution, today
and tomorrow).  An API key from a paid plan is optional.

Example config::

    forecasts:
      pv:
        type: forecast_solar
        latitude: 52.52
        longitude: 13.44
        planes:
          - declination: 20     # tilt in degrees, 0 = flat, 90 = vertical
            azimuth: 50         # -180 = north, -90 = east, 0 = south, 90 = west
            kwp: 7.4
        # damping_morning: 0    # 0..1, optional (ioBroker "dampm")
        # damping_evening: 0    # 0..1, optional (ioBroker "dampe")
        # api_key: !secret forecast_solar_key   # optional, paid plans only
        # api_url: https://api.forecast.solar   # optional, e.g. a local replay server for tests
        # refresh_minutes: 60   # per plane; keep plane count × 60 / refresh ≤ 12

A single plane can also be written without ``planes:``, with ``declination``,
``azimuth`` and ``kwp`` at the top level.  With several planes, one request
per plane is made and the forecasts are summed.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)

_MIN_REFRESH_MIN = 10.0


_PLANE = {
    "declination": ParamSpec(float, required=True, minimum=0, maximum=90, label="Tilt (°)",
                             description="Tilt of the modules: 0 is flat, 90 is vertical"),
    "azimuth": ParamSpec(float, required=True, minimum=-180, maximum=180, label="Azimuth (°)",
                         description="Direction the modules face: -180 is north, -90 east, 0 "
                                     "south, 90 west"),
    "kwp": ParamSpec(float, required=True, minimum=0, exclusive_minimum=True, label="Peak power (kWp)",
                     description="Rated power of the modules on this plane, from the data "
                                 "sheet of your system"),
}


def register(registry: PluginRegistry) -> None:
    registry.register_forecast(
        "forecast_solar", _build,
        title="Forecast.Solar",
        description="PV generation forecast from forecast.solar.",
        scopes=("system",),
        settings={
            "latitude": ParamSpec(float, required=True, minimum=-90, maximum=90,
                                  description="Latitude of the PV system in degrees, e.g. "
                                              "49.61. North is positive"),
            "longitude": ParamSpec(float, required=True, minimum=-180, maximum=180,
                                   description="Longitude of the PV system in degrees, e.g. "
                                               "6.13. East is positive"),
            "planes": ParamSpec(list, label="Roof planes", item_label="roof plane",
                                description="One entry for every roof side or group of "
                                            "modules that faces its own way. Their forecasts "
                                            "are added up",
                                items=ParamSpec(dict, fields=_PLANE)),
            # A single plane may be written at the top level instead of 'planes'.
            **{name: dataclasses.replace(spec, required=False, legacy=True,
                                         description=f"Single plane: {spec.description}")
               for name, spec in _PLANE.items()},
            "damping_morning": ParamSpec(float, minimum=0, maximum=1, default=0, advanced=True,
                                         label="Morning damping",
                                         description="Lowers the forecast for the morning, "
                                                     "e.g. when the modules are shaded then: 0 "
                                                     "leaves it as it is, 1 damps it most"),
            "damping_evening": ParamSpec(float, minimum=0, maximum=1, default=0, advanced=True,
                                         label="Evening damping",
                                         description="Lowers the forecast for the evening: 0 "
                                                     "leaves it as it is, 1 damps it most"),
            "refresh_minutes": ParamSpec(float, minimum=_MIN_REFRESH_MIN, default=60, advanced=True,
                                         label="Fetch every (min)",
                                         description="Minutes between two requests for each "
                                                     "plane. The free plan of Forecast.Solar "
                                                     "allows 12 requests an hour"),
            "api_key": ParamSpec(str, secret=True, advanced=True,
                                 description="Only with a paid plan of Forecast.Solar: its API "
                                             "key"),
            "api_url": ParamSpec(str, advanced=True, description="Tests only"),
        },
    )


def _num(value: Any) -> float:
    """Parse a number, accepting a decimal comma ("52,5176") as ioBroker stores it."""
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    return float(value)


def _build(forecast_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .forecast import ForecastSolarForecast, Plane

    try:
        latitude = _num(cfg["latitude"])
        longitude = _num(cfg["longitude"])
    except (KeyError, TypeError, ValueError):
        _log.warning(
            "Forecast %r (forecast_solar): 'latitude' and 'longitude' are required — skipping",
            forecast_id,
        )
        return None

    raw_planes = cfg.get("planes")
    if raw_planes is None:
        raw_planes = [cfg]
    if not isinstance(raw_planes, list) or not raw_planes:
        _log.warning("Forecast %r (forecast_solar): 'planes' must be a non-empty list — skipping",
                     forecast_id)
        return None

    planes: list[Plane] = []
    for i, p in enumerate(raw_planes):
        try:
            planes.append(Plane(
                declination=_num(p["declination"]),
                azimuth=_num(p["azimuth"]),
                kwp=_num(p["kwp"]),
            ))
        except (KeyError, TypeError, ValueError):
            _log.warning(
                "Forecast %r (forecast_solar): plane %d needs numeric 'declination', "
                "'azimuth' and 'kwp' — skipping forecast",
                forecast_id, i,
            )
            return None

    refresh_min = _num(cfg.get("refresh_minutes", 60))
    if refresh_min < _MIN_REFRESH_MIN:
        _log.warning("Forecast %r (forecast_solar): refresh_minutes %.0f too low — using %.0f",
                     forecast_id, refresh_min, _MIN_REFRESH_MIN)
        refresh_min = _MIN_REFRESH_MIN

    return ForecastSolarForecast(
        forecast_id=forecast_id,
        latitude=latitude,
        longitude=longitude,
        planes=planes,
        damping_morning=_num(cfg.get("damping_morning", 0)),
        damping_evening=_num(cfg.get("damping_evening", 0)),
        api_key=str(cfg.get("api_key") or "").strip() or None,
        api_url=str(cfg.get("api_url") or "").strip() or None,
        refresh_s=refresh_min * 60.0,
    )

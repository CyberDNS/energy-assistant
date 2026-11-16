"""forecast_solar plugin — PV generation forecast directly from Forecast.Solar.

No ioBroker needed: the forecast comes from https://forecast.solar, the same
service the ioBroker pvforecast adapter uses.  The public API needs no account
(12 requests per hour per IP, one plane per request, hourly resolution, today
and tomorrow).  An API key from a paid plan is optional.

Example config::

    forecasts:
      pv:
        type: forecast_solar
        latitude: 49.82
        longitude: 6.44
        planes:
          - declination: 20     # tilt in degrees, 0 = flat, 90 = vertical
            azimuth: 50         # -180 = north, -90 = east, 0 = south, 90 = west
            kwp: 7.4
        # damping_morning: 0    # 0..1, optional (ioBroker "dampm")
        # damping_evening: 0    # 0..1, optional (ioBroker "dampe")
        # api_key: !secret forecast_solar_key   # optional, paid plans only
        # refresh_minutes: 60   # per plane; keep plane count × 60 / refresh ≤ 12

A single plane can also be written without ``planes:``, with ``declination``,
``azimuth`` and ``kwp`` at the top level.  With several planes, one request
per plane is made and the forecasts are summed.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)

_MIN_REFRESH_MIN = 10.0


def register(registry: PluginRegistry) -> None:
    registry.register_forecast("forecast_solar", _build)


def _num(value: Any) -> float:
    """Parse a number, accepting a decimal comma ("49,8176") as ioBroker stores it."""
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
        refresh_s=refresh_min * 60.0,
    )

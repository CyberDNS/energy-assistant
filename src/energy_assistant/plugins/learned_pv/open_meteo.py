"""Open-Meteo client — hourly irradiance forecasts and the archive of past forecasts.

Two endpoints of https://open-meteo.com (free for non-commercial use, no key):

* ``api.open-meteo.com/v1/forecast`` — the current forecast, up to 16 days
  ahead (``forecast_days``) plus the hours already past today
  (``past_days``);
* ``historical-forecast-api.open-meteo.com/v1/forecast`` — the archive of
  past forecasts, used to train on what a forecast *said* rather than what
  happened.

Radiation values are the mean of the hour *ending* at their timestamp; they
are returned here keyed by the hour they *start*, like the PV statistics.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from ...core.pv_model import WeatherSeries

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

_HOURLY = ("shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation",
           "temperature_2m", "snow_depth", "cloud_cover")


class OpenMeteoError(RuntimeError):
    """The Open-Meteo request failed or returned an error."""


def parse_hourly(body: dict[str, Any]) -> tuple[WeatherSeries, list[float | None]]:
    """``(weather, cloud cover %)`` from an Open-Meteo answer requested with
    ``timezone=UTC``; hours without irradiance are dropped."""
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    cols = {name: hourly.get(name) or [None] * len(times) for name in _HOURLY}
    start, ghi, dni, dhi, temp, snow, cloud = [], [], [], [], [], [], []
    for i, t in enumerate(times):
        g, n, d = cols["shortwave_radiation"][i], cols["direct_normal_irradiance"][i], cols["diffuse_radiation"][i]
        if g is None or n is None or d is None:
            continue
        ts = datetime.fromisoformat(t)
        ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
        start.append(ts - timedelta(hours=1))
        ghi.append(float(g))
        dni.append(float(n))
        dhi.append(float(d))
        temp.append(cols["temperature_2m"][i])
        snow.append(cols["snow_depth"][i])
        cloud.append(cols["cloud_cover"][i])
    return WeatherSeries(start, ghi, dni, dhi, temp, snow), cloud


class OpenMeteoClient:
    """Fetches hourly irradiance for one location."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_url: str | None = None,
        archive_url: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._lat = latitude
        self._lon = longitude
        self._forecast_url = forecast_url or FORECAST_URL
        self._archive_url = archive_url or ARCHIVE_URL
        self._http = http or httpx.AsyncClient(timeout=60.0)

    async def forecast(self, days: int = 7, past_days: int = 1) -> tuple[WeatherSeries, list[float | None]]:
        """The current forecast: *past_days* back, *days* ahead (incl. today)."""
        return await self._get(self._forecast_url, {"forecast_days": days, "past_days": past_days})

    async def archive(self, start: date, end: date) -> WeatherSeries:
        """Past forecasts for the local days *start* … *end* (inclusive)."""
        weather, _ = await self._get(self._archive_url, {
            "start_date": start.isoformat(), "end_date": end.isoformat(),
        })
        return weather

    async def _get(self, url: str, extra: dict[str, Any]) -> tuple[WeatherSeries, list[float | None]]:
        params = {
            "latitude": self._lat, "longitude": self._lon,
            "hourly": ",".join(_HOURLY), "timezone": "UTC", **extra,
        }
        try:
            resp = await self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise OpenMeteoError(f"request failed: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or body.get("error"):
            raise OpenMeteoError(f"HTTP {resp.status_code}: {body.get('reason') or resp.text[:200]}")
        return parse_hourly(body)

    async def aclose(self) -> None:
        await self._http.aclose()

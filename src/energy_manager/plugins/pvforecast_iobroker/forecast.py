"""
PV generation forecast backed by the ioBroker *pvforecast* adapter.

The pvforecast adapter publishes per-hour estimated PV power (Watts) under OIDs:

    pvforecast.0.plants.{plant_id}.power.hoursToday.{HH:MM:SS}
    pvforecast.0.plants.{plant_id}.power.hoursTomorrow.{HH:MM:SS}

Each value is the forecast average power (W) for that 1-hour slot in **local
time** (the time zone of the ioBroker host).

Usage::

    from zoneinfo import ZoneInfo
    from energy_manager.plugins._iobroker.client import IoBrokerClient
    from energy_manager.plugins.pvforecast_iobroker.forecast import PVForecastIoBrokerForecast

    async with IoBrokerClient("192.168.2.30") as client:
        forecast = PVForecastIoBrokerForecast(client, tz=ZoneInfo("Europe/Berlin"))
        points = await forecast.get_forecast(timedelta(hours=24))
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .._iobroker.client import IoBrokerClientProtocol
from ...core.models import ForecastPoint, ForecastQuantity

# pvforecast publishes power for hours 0–23, but only populates daylight slots.
_ALL_HOURS = list(range(0, 24))
_OID_TIME_FMT = "{:02d}:00:00"


class PVForecastIoBrokerForecast:
    """
    ForecastProvider that reads hourly PV power (W) from the pvforecast ioBroker adapter.

    Parameters
    ----------
    client:
        An open ``IoBrokerClient`` (or anything implementing ``IoBrokerClientProtocol``).
    plant_id:
        The pvforecast plant identifier — appears as the folder name under
        ``pvforecast.0.plants.*``.  Defaults to ``"pv"`` (the adapter default).
    tz:
        Local timezone used by the pvforecast adapter for its hour labels.
        Defaults to ``Europe/Berlin``.  Pass any ``ZoneInfo`` to override.
    """

    def __init__(
        self,
        client: IoBrokerClientProtocol,
        plant_id: str = "pv",
        tz: ZoneInfo | None = None,
    ) -> None:
        self._client = client
        self._plant_id = plant_id
        try:
            self._tz = tz or ZoneInfo("Europe/Berlin")
        except ZoneInfoNotFoundError:
            self._tz = timezone.utc  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # ForecastProvider protocol
    # ------------------------------------------------------------------

    @property
    def quantity(self) -> ForecastQuantity:
        return ForecastQuantity.PV_GENERATION

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """
        Return forecast PV power (W) at hourly boundaries within *horizon*.

        Timestamps are UTC.  Hours with no forecast data (nights, or slots
        beyond what the adapter published) are omitted rather than returning
        zero — callers should treat missing hours as 0 W.
        """
        now_utc = datetime.now(timezone.utc)
        cutoff_utc = now_utc + horizon

        # Build the set of (day_label, local_date) pairs we need to query.
        now_local = now_utc.astimezone(self._tz)
        today_local = now_local.date()
        tomorrow_local = today_local + timedelta(days=1)

        oid_map: dict[str, datetime] = {}  # oid → UTC datetime for that slot

        for day_label, local_date in [
            ("hoursToday", today_local),
            ("hoursTomorrow", tomorrow_local),
        ]:
            for hour in _ALL_HOURS:
                local_dt = datetime(
                    local_date.year, local_date.month, local_date.day,
                    hour, 0, 0,
                    tzinfo=self._tz,
                )
                utc_dt = local_dt.astimezone(timezone.utc)
                # Only include slots that fall within [now, now+horizon)
                if utc_dt < now_utc or utc_dt >= cutoff_utc:
                    continue
                oid = (
                    f"pvforecast.0.plants.{self._plant_id}"
                    f".power.{day_label}.{_OID_TIME_FMT.format(hour)}"
                )
                oid_map[oid] = utc_dt

        if not oid_map:
            return []

        # Bulk-fetch all OIDs in a single round-trip.
        raw = await self._client.get_bulk(list(oid_map.keys()))

        points: list[ForecastPoint] = []
        for oid, utc_dt in oid_map.items():
            value = raw.get(oid)
            if value is not None:
                try:
                    points.append(ForecastPoint(timestamp=utc_dt, value=float(value)))
                except (TypeError, ValueError):
                    pass  # skip malformed values

        return sorted(points, key=lambda p: p.timestamp)

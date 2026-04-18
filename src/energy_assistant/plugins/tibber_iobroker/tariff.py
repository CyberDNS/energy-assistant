"""TibberIoBrokerTariff — reads Tibber spot prices from the ioBroker tibberlink adapter.

Required ioBroker objects
--------------------------
- ``tibberlink.0.Homes.<HOME_ID>.CurrentPrice.total``   (current hour, app-aligned)
- ``tibberlink.0.Homes.<HOME_ID>.PricesToday.json``     (full day schedule)
- ``tibberlink.0.Homes.<HOME_ID>.PricesTomorrow.json``  (optional, next day)

``price_at()`` prefers ``CurrentPrice.total`` for the current hour — a
single lightweight OID read. ``price_schedule()`` parses the JSON arrays and
uses each entry's ``total`` value.

Fallback
--------
When ioBroker is unreachable or the OIDs return no data, a zero-price
schedule is returned so the optimizer can still run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ...core.models import TariffPoint
from .._iobroker.client import IoBrokerClientProtocol

_log = logging.getLogger(__name__)

_CURRENT_PRICE_OID = "tibberlink.0.Homes.{home_id}.CurrentPrice.total"
_TODAY_OID = "tibberlink.0.Homes.{home_id}.PricesToday.json"
_TOMORROW_OID = "tibberlink.0.Homes.{home_id}.PricesTomorrow.json"


class TibberIoBrokerTariff:
    """A ``TariffModel`` that reads Tibber spot prices from ioBroker tibberlink.

    Implements the ``TariffModel`` protocol structurally (no inheritance).

    Parameters
    ----------
    tariff_id:
        Stable identifier, e.g. ``"tibber"`` or ``"household"``.
    client:
        An open ioBroker client (or pool result).
    home_id:
        Your Tibber home ID, found under ``tibberlink.0.Homes.*`` in ioBroker.
    """

    def __init__(
        self,
        tariff_id: str,
        client: IoBrokerClientProtocol,
        home_id: str,
    ) -> None:
        self._tariff_id = tariff_id
        self._client = client
        self._home_id = home_id

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    async def price_at(self, dt: datetime) -> float:
        """Return the Tibber price in EUR/kWh at *dt*.

        For the **current hour** prefers the lightweight ``CurrentPrice.total``
        OID (a single float) instead of parsing the full JSON schedule.
        Falls back to the JSON schedule when ``dt`` is in the future, or if the
        OID read fails.
        """
        now_h = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        target_h = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

        if target_h == now_h:
            oid = _CURRENT_PRICE_OID.format(home_id=self._home_id)
            try:
                raw = await self._client.get_value(oid)
                if raw is not None:
                    return float(raw)
            except Exception:
                _log.warning("Failed to read CurrentPrice from %r — falling back", oid)

        # Fall back to full schedule (future hours or CurrentPrice unavailable)
        schedule = await self.price_schedule(timedelta(hours=48))
        for point in schedule:
            point_utc = point.timestamp.astimezone(timezone.utc).replace(
                minute=0, second=0, microsecond=0
            )
            if point_utc == target_h:
                return point.price_eur_per_kwh
        return 0.0

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Return hourly Tibber price points covering *horizon*.

        Falls back to a zero schedule when live data is unavailable.
        """
        points: list[TariffPoint] = []

        for oid_template in (_TODAY_OID, _TOMORROW_OID):
            oid = oid_template.format(home_id=self._home_id)
            try:
                raw = await self._client.get_value(oid)
                if raw is None:
                    continue
                entries = json.loads(raw) if isinstance(raw, str) else raw
                for entry in entries or []:
                    starts_at = entry.get("startsAt")
                    total = entry.get("total")
                    if starts_at is None or total is None:
                        continue
                    _ts = datetime.fromisoformat(starts_at)
                    _ts_utc = (
                        _ts.astimezone(timezone.utc)
                        if _ts.tzinfo is not None
                        else _ts.replace(tzinfo=timezone.utc)
                    )
                    points.append(
                        TariffPoint(
                            timestamp=_ts_utc,
                            price_eur_per_kwh=float(total),
                        )
                    )
            except Exception:
                _log.warning("Failed to read Tibber prices from %r", oid, exc_info=True)

        if not points:
            _log.warning("No Tibber prices available — returning zero schedule")
            now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            hours = int(horizon.total_seconds() / 3600) + 1
            return [
                TariffPoint(
                    timestamp=now + timedelta(hours=i),
                    price_eur_per_kwh=0.0,
                )
                for i in range(hours)
            ]

        points.sort(key=lambda p: p.timestamp)
        return points

    async def export_price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Tibber is an import-only tariff; export price is always zero."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = int(horizon.total_seconds() / 3600) + 1
        return [
            TariffPoint(
                timestamp=now + timedelta(hours=i),
                price_eur_per_kwh=0.0,
            )
            for i in range(hours)
        ]




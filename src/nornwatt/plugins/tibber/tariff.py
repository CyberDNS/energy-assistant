"""TibberTariff — Tibber spot prices straight from the Tibber API.

Prices are the ``total`` value (energy + taxes + Tibber fee) of
``priceInfo.today`` and ``priceInfo.tomorrow`` at the configured resolution
(15-minute by default).  Caching and the faster retry while tomorrow's prices
are pending live in ``TibberApi``.

When no prices are available a zero schedule is returned, matching the
ioBroker-based tariff, so the server falls back to other tariffs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ...core.models import TariffPoint
from .api import TibberApi

_log = logging.getLogger(__name__)


def _zero_schedule(horizon: timedelta) -> list[TariffPoint]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hours = int(horizon.total_seconds() / 3600) + 1
    return [TariffPoint(timestamp=now + timedelta(hours=i), price_eur_per_kwh=0.0) for i in range(hours)]


class TibberTariff:
    """A ``TariffModel`` backed by the Tibber GraphQL API."""

    def __init__(self, tariff_id: str, api: TibberApi, home_id: str | None = None) -> None:
        self._tariff_id = tariff_id
        self._api = api
        self._home_id = home_id

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    async def _points(self) -> list[TariffPoint]:
        try:
            home_id = await self._api.resolve_home_id(self._home_id)
            prices = await self._api.prices(home_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Tariff %r: Tibber prices unavailable: %s", self._tariff_id, exc)
            return []
        return prices.points if prices else []

    async def price_at(self, dt: datetime) -> float:
        """Price of the slot containing *dt* (0.0 when unknown)."""
        points = await self._points()
        target = dt.astimezone(timezone.utc)
        for cur, nxt in zip(points, points[1:] + [None]):
            end = nxt.timestamp if nxt else cur.timestamp + (
                points[-1].timestamp - points[-2].timestamp if len(points) > 1 else timedelta(hours=1)
            )
            if cur.timestamp <= target < end:
                return cur.price_eur_per_kwh
        return 0.0

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        points = await self._points()
        if not points:
            _log.warning("Tariff %r: no Tibber prices available — returning zero schedule", self._tariff_id)
            return _zero_schedule(horizon)
        return list(points)

    async def export_price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Tibber is an import-only tariff; export price is always zero."""
        return _zero_schedule(horizon)

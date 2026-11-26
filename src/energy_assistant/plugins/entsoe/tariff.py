"""EntsoeTariff — ENTSO-E day-ahead spot price plus fees, taxes and VAT.

Import price per market time unit (15 or 60 minutes)::

    price = (spot + markup + grid_fee + taxes) × (1 + vat)

``spot`` is the day-ahead price converted from EUR/MWh to EUR/kWh; the
three surcharges are in EUR/kWh, ``vat`` is a fraction (0.19 = 19 %).  VAT
is applied to the whole sum, so also to the spot part and also when the
sum is negative (a negative price stays negative, by the VAT factor).

Export (feed-in) price:

* ``none`` — nothing is paid (0),
* ``spot`` — ``spot × factor + offset`` (no fees, taxes or VAT),
* ``fixed`` — a constant ``price_eur_per_kwh``.

The schedule covers the published prices only (today and, from about
13:00 CET, tomorrow); the server extends it with estimated prices beyond
that, flagged as estimated, as for Tibber.  Without any prices a zero
schedule is returned, so the server falls back to other tariffs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ...core.models import TariffPoint
from .api import EntsoeApi

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Formula:
    markup_eur_per_kwh: float = 0.0
    grid_fee_eur_per_kwh: float = 0.0
    taxes_eur_per_kwh: float = 0.0
    vat: float = 0.0

    def import_price(self, spot: float) -> float:
        net = spot + self.markup_eur_per_kwh + self.grid_fee_eur_per_kwh + self.taxes_eur_per_kwh
        return net * (1.0 + self.vat)


@dataclass(frozen=True)
class ExportRule:
    type: str = "none"              # none | spot | fixed
    factor: float = 1.0
    offset_eur_per_kwh: float = 0.0
    price_eur_per_kwh: float = 0.0

    def export_price(self, spot: float) -> float:
        if self.type == "spot":
            return spot * self.factor + self.offset_eur_per_kwh
        if self.type == "fixed":
            return self.price_eur_per_kwh
        return 0.0


def _hourly(horizon: timedelta, price: float) -> list[TariffPoint]:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    hours = int(horizon.total_seconds() / 3600) + 1
    return [TariffPoint(timestamp=now + timedelta(hours=i), price_eur_per_kwh=price)
            for i in range(hours)]


class EntsoeTariff:
    """A ``TariffModel`` on ENTSO-E day-ahead prices."""

    def __init__(
        self,
        tariff_id: str,
        api: EntsoeApi,
        formula: Formula | None = None,
        export: ExportRule | None = None,
    ) -> None:
        self._tariff_id = tariff_id
        self._api = api
        self._formula = formula or Formula()
        self._export = export or ExportRule()

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    async def _spot(self) -> list[TariffPoint]:
        try:
            prices = await self._api.prices()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Tariff %r: ENTSO-E prices unavailable: %s", self._tariff_id, exc)
            return []
        return prices.points if prices else []

    async def _spot_at(self, dt: datetime) -> float | None:
        points = await self._spot()
        if not points:
            return None
        target = dt.astimezone(UTC)
        step = (points[1].timestamp - points[0].timestamp) if len(points) > 1 \
            else timedelta(hours=1)
        for cur, nxt in zip(points, [*points[1:], None], strict=True):
            end = nxt.timestamp if nxt else cur.timestamp + step
            if cur.timestamp <= target < end:
                return cur.price_eur_per_kwh
        return None

    async def price_at(self, dt: datetime) -> float:
        """Import price of the slot containing *dt* (0.0 when unknown)."""
        spot = await self._spot_at(dt)
        return 0.0 if spot is None else self._formula.import_price(spot)

    async def export_price_at(self, dt: datetime) -> float:
        if self._export.type != "spot":
            return self._export.export_price(0.0)
        spot = await self._spot_at(dt)
        return 0.0 if spot is None else self._export.export_price(spot)

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        points = await self._spot()
        if not points:
            _log.warning("Tariff %r: no ENTSO-E prices available — returning zero schedule",
                         self._tariff_id)
            return _hourly(horizon, 0.0)
        return [TariffPoint(timestamp=p.timestamp,
                            price_eur_per_kwh=self._formula.import_price(p.price_eur_per_kwh))
                for p in points]

    async def export_price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        if self._export.type != "spot":
            return _hourly(horizon, self._export.export_price(0.0))
        points = await self._spot()
        if not points:
            return _hourly(horizon, 0.0)
        return [TariffPoint(timestamp=p.timestamp,
                            price_eur_per_kwh=self._export.export_price(p.price_eur_per_kwh))
                for p in points]

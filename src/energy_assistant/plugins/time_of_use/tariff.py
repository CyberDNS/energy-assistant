"""TimeOfUseTariff — fixed prices by time window, weekday and season.

Standalone it is a complete tariff (classic day/night tariff).  With a
``base`` tariff it is a **component** on top of that tariff: its price is
added to the base price at every moment, e.g. time-variable grid fees
(§14a EnWG, Modul 3) on top of a spot price.  A component's prices may be
negative (a fee reduction in a low window).

Schedules are on a 15-minute grid (window times are on quarter hours).  A
composed schedule covers the base schedule's time span — so it ends where the
base's known prices end and the server's usual handling of missing future
prices still applies — on the base's own points plus every quarter hour.
A base without any import price (all zero: prices unavailable) is passed on
unchanged, so the server still sees that there are no prices.
"""

from __future__ import annotations

import bisect
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from ...core.models import TariffPoint
from .schedule import QUARTER, TouSchedule


def _grid(start: datetime, end: datetime) -> list[datetime]:
    out: list[datetime] = []
    t = start
    while t < end:
        out.append(t)
        t += QUARTER
    return out


def _floor_quarter(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC).replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % 15)


class TimeOfUseTariff:
    """A ``TariffModel`` with a time-of-use table, optionally added to a *base*."""

    def __init__(
        self,
        tariff_id: str,
        schedule: TouSchedule,
        export_price_eur_per_kwh: float = 0.0,
        base: Any = None,
    ) -> None:
        self._tariff_id = tariff_id
        self._schedule = schedule
        self._export_price = export_price_eur_per_kwh
        self._base = base

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    @property
    def schedule(self) -> TouSchedule:
        return self._schedule

    @property
    def base(self) -> Any:
        return self._base

    def period_at(self, dt: datetime) -> str:
        """Name of the period in force at *dt*."""
        return self._schedule.period_at(dt)

    async def price_at(self, dt: datetime) -> float:
        own = self._schedule.price_at(dt)
        if self._base is None:
            return own
        return await self._base.price_at(dt) + own

    async def export_price_at(self, dt: datetime) -> float:
        if self._base is None:
            return self._export_price
        base_fn = getattr(self._base, "export_price_at", None)
        base = await base_fn(dt) if base_fn is not None else 0.0
        return base + self._export_price

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        if self._base is None:
            return self._own_schedule(horizon, self._schedule.price_at)
        base = await self._base.price_schedule(horizon)
        if not any(abs(p.price_eur_per_kwh) > 0.001 for p in base):
            return list(base)
        return _compose(base, self._schedule.price_at)

    async def export_price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        if self._base is None:
            return self._own_schedule(horizon, lambda _dt: self._export_price)
        base = await self._base.export_price_schedule(horizon)
        if not self._export_price:
            return list(base)
        return _compose(base, lambda _dt: self._export_price)

    @staticmethod
    def _own_schedule(horizon: timedelta, price: Any) -> list[TariffPoint]:
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        times = _grid(now, now + horizon + timedelta(hours=1))
        return [TariffPoint(timestamp=t, price_eur_per_kwh=price(t)) for t in times]


def _compose(base: list[TariffPoint], extra: Any) -> list[TariffPoint]:
    """*base* plus ``extra(t)``, on base's points and every quarter hour of its span."""
    pts = sorted(base, key=lambda p: p.timestamp)
    if not pts:
        return []
    times = [p.timestamp for p in pts]
    step = min((b - a for a, b in pairwise(times) if b > a), default=timedelta(hours=1))
    end = times[-1] + step
    grid = sorted(set(times) | set(_grid(_floor_quarter(times[0]), end)))
    out: list[TariffPoint] = []
    for t in grid:
        i = bisect.bisect_right(times, t) - 1
        if i < 0:
            continue  # before the first base point
        out.append(TariffPoint(timestamp=t,
                               price_eur_per_kwh=pts[i].price_eur_per_kwh + extra(t)))
    return out

"""Price curve for dashboards — import, feed-in and effective price per slot.

The *effective price* is what one extra kWh of consumption costs in a slot,
given the PV and consumption forecasts:

* while the site imports from the grid, an extra kWh is bought at the import
  price;
* while PV covers the load and feeds in, an extra kWh is PV that is no longer
  sold, so it costs the feed-in price (lost income);
* in between it is blended by the share of the load PV does not cover::

      s = clamp((load - pv) / load, 0, 1)
      effective = s * import_price + (1 - s) * feed_in_price

It is a display figure only.  The optimizer does not use it: it plans PV,
storage and loads jointly against the raw prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class PriceSlot:
    timestamp: datetime
    import_price: float
    feed_in_price: float
    effective_price: float | None   # None when the forecasts for the slot are unknown
    estimated: bool                 # import price is an extrapolation, not published yet


def effective_price(
    import_price: float, feed_in_price: float, pv_kw: float, load_kw: float
) -> float:
    """Effective price of one extra kWh (see module docstring)."""
    pv = max(0.0, pv_kw)
    load = max(0.0, load_kw)
    if load <= 0.0:
        share = 0.0 if pv > 0.0 else 1.0
    else:
        share = min(1.0, max(0.0, (load - pv) / load))
    return share * import_price + (1.0 - share) * feed_in_price


def build_price_slots(
    timestamps: list[datetime],
    import_prices: list[float],
    feed_in_prices: list[float],
    pv_kw: list[float],
    load_kw: list[float],
    estimated: list[bool],
) -> list[PriceSlot]:
    """Combine aligned per-slot series into ``PriceSlot`` rows."""
    return [
        PriceSlot(
            timestamp=ts,
            import_price=ip,
            feed_in_price=fp,
            effective_price=effective_price(ip, fp, pv, load),
            estimated=est,
        )
        for ts, ip, fp, pv, load, est in zip(
            timestamps, import_prices, feed_in_prices, pv_kw, load_kw, estimated
        )
    ]


class PriceCurveHistory:
    """Keeps already-passed slots so a dashboard can show the whole day.

    Each planning cycle starts at "now", so its slots never cover the morning.
    ``update`` replaces every slot from the new plan's first timestamp on and
    keeps older ones for ``keep`` (default 24 h).
    """

    def __init__(self, keep: timedelta = timedelta(hours=24)) -> None:
        self._keep = keep
        self._slots: dict[datetime, PriceSlot] = {}

    def update(
        self,
        slots: list[PriceSlot],
        now: datetime,
        past_prices: list[tuple[datetime, float]] | None = None,
    ) -> list[PriceSlot]:
        """Merge *slots* and return the full curve, oldest first.

        *past_prices* (timestamp, import price) fills slots before the plan
        that were never seen — e.g. the morning after a restart.  They carry
        no effective price because the forecasts for them are gone.
        """
        cutoff = now - self._keep
        if slots:
            first = min(s.timestamp for s in slots)
            self._slots = {ts: s for ts, s in self._slots.items() if ts < first}
        for ts, price in past_prices or []:
            if cutoff <= ts and ts not in self._slots and (not slots or ts < first):
                self._slots[ts] = PriceSlot(ts, price, 0.0, None, False)
        for s in slots:
            self._slots[s.timestamp] = s
        self._slots = {ts: s for ts, s in self._slots.items() if ts >= cutoff}
        return [self._slots[ts] for ts in sorted(self._slots)]


def price_curve_payload(slots: list[PriceSlot], step_minutes: int) -> dict:
    """Compact JSON attributes for Home Assistant.

    Rows are arrays to stay well under the recorder's 16 kB attribute limit
    (two days of 15-min slots are about 8 kB).  Timestamps are epoch
    milliseconds, which ApexCharts uses as-is.
    """
    def r(v: float | None) -> float | None:
        return None if v is None else round(v, 4)

    return {
        "step_minutes": step_minutes,
        "columns": ["time", "price", "effective", "feed_in", "estimated"],
        "data": [
            [
                int(s.timestamp.timestamp() * 1000),
                r(s.import_price),
                r(s.effective_price),
                r(s.feed_in_price) if s.effective_price is not None else None,
                1 if s.estimated else 0,
            ]
            for s in slots
        ],
    }


def current_slot(slots: list[PriceSlot], now: datetime) -> PriceSlot | None:
    """The slot containing *now* (the last one that started at or before it)."""
    current = None
    for s in slots:
        if s.timestamp <= now:
            current = s
        else:
            break
    return current

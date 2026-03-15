"""
Tibber via ioBroker (tibberlink adapter) — tariff plugin.

Reads electricity prices from the ioBroker **tibberlink** adapter, which
provides today's and tomorrow's 15-minute price slots as JSON arrays.

Required ioBroker objects
--------------------------
- ``tibberlink.0.Homes.<HOME_ID>.PricesToday.json``
- ``tibberlink.0.Homes.<HOME_ID>.PricesTomorrow.json``  (optional but recommended)

Both objects hold a JSON array whose elements look like::

    {
        "startsAt": "2026-03-15T08:00:00.000+01:00",
        "total": 0.3719,
        "energy": 0.136,
        "tax": 0.2359,
        "currency": "EUR",
        "level": "EXPENSIVE"
    }

Only ``startsAt`` and ``total`` are used; all other fields are ignored.

Configuration example (YAML)
-----------------------------
.. code-block:: yaml

    tariffs:
      - id: tibber
        plugin: energy_manager.plugins.tibber_iobroker
        data:
          host: 192.168.2.30
          port: 8087
          home_id: aa115263-6d29-4e80-8190-fb95ddd4e743
          # api_token: !secret iobroker_token   # optional

Connection sharing
------------------
Pass an ``IoBrokerConnectionPool`` instance so that other ioBroker-based
plugins on the same host reuse the same HTTP connection::

    from energy_manager.plugins._iobroker.pool import IoBrokerConnectionPool

    pool = IoBrokerConnectionPool()
    tariff = TibberIoBrokerTariff(
        tariff_id="tibber",
        client=pool.get("192.168.2.30"),
        home_id="aa115263-...",
    )
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from ...core.models import TariffPoint
from .._iobroker.client import IoBrokerClientProtocol

_log = logging.getLogger(__name__)

_BASE_OID = "tibberlink.0.Homes.{home_id}"
_TODAY_OID = _BASE_OID + ".PricesToday.json"
_TOMORROW_OID = _BASE_OID + ".PricesTomorrow.json"


class TibberIoBrokerTariff:
    """
    A ``TariffModel`` that reads Tibber spot prices from the ioBroker
    tibberlink adapter.

    Implements the ``TariffModel`` protocol structurally.

    Parameters
    ----------
    tariff_id:
        Stable identifier, e.g. ``"tibber"``.
    client:
        An open ``IoBrokerClientProtocol`` instance.  Use
        ``IoBrokerConnectionPool.get()`` to share it with other plugins.
    home_id:
        Tibber home UUID as shown in the tibberlink adapter object tree.
    include_tomorrow:
        When ``True`` (default), the tomorrow schedule is merged in so that
        ``price_schedule`` can cover horizons beyond midnight.
    """

    def __init__(
        self,
        tariff_id: str,
        client: IoBrokerClientProtocol,
        home_id: str,
        include_tomorrow: bool = True,
        _now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self._tariff_id = tariff_id
        self._client = client
        self._today_oid = _TODAY_OID.format(home_id=home_id)
        self._tomorrow_oid = _TOMORROW_OID.format(home_id=home_id) if include_tomorrow else None
        self._now_func: Callable[[], datetime] = _now_func or (
            lambda: datetime.now(timezone.utc)
        )

    # ------------------------------------------------------------------
    # TariffModel protocol
    # ------------------------------------------------------------------

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    async def price_at(self, dt: datetime) -> float:
        """
        Return the slot price active at *dt*.

        Scans today (and tomorrow) schedules for the most recently started
        slot at or before *dt*.  Returns ``0.0`` if no matching slot is found.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        entries = await self._fetch_all_entries()
        best_starts: datetime | None = None
        best_price = 0.0
        for entry in entries:
            try:
                starts_at = datetime.fromisoformat(entry["startsAt"])
                if starts_at.tzinfo is None:
                    starts_at = starts_at.replace(tzinfo=timezone.utc)
                price = float(entry["total"])
            except (KeyError, TypeError, ValueError):
                continue
            if starts_at <= dt and (best_starts is None or starts_at > best_starts):
                best_starts = starts_at
                best_price = price
        return best_price

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """
        Return price points covering the given *horizon*.

        15-minute slot granularity is preserved from the tibberlink data.
        Falls back to a flat zero schedule if no data is available.
        """
        schedule = await self._parse_schedule(horizon)
        if schedule:
            return schedule
        # No data available — flat zero schedule so callers always get a result.
        return self._zero_schedule(horizon)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch_all_entries(self) -> list[dict]:
        """Fetch and concatenate raw JSON entries from today (+ tomorrow) OIDs."""
        oids = [self._today_oid]
        if self._tomorrow_oid:
            oids.append(self._tomorrow_oid)

        all_entries: list[dict] = []
        for oid in oids:
            raw = await self._client.get_value(oid)
            if raw is None:
                continue
            try:
                entries = json.loads(raw) if isinstance(raw, str) else list(raw)
                if isinstance(entries, list):
                    all_entries.extend(entries)
            except (json.JSONDecodeError, TypeError) as exc:
                _log.warning("Could not parse schedule from '%s': %s", oid, exc)
        return all_entries

    async def _parse_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Return TariffPoints within [now, now+horizon), sorted ascending."""
        entries = await self._fetch_all_entries()
        now = self._now_func()
        cutoff = now + horizon
        points: list[TariffPoint] = []

        for entry in entries:
            try:
                starts_at = datetime.fromisoformat(entry["startsAt"])
                if starts_at.tzinfo is None:
                    starts_at = starts_at.replace(tzinfo=timezone.utc)
                price = float(entry["total"])
            except (KeyError, TypeError, ValueError) as exc:
                _log.debug("Skipping malformed entry %r: %s", entry, exc)
                continue

            if now <= starts_at < cutoff:
                points.append(TariffPoint(timestamp=starts_at, price_eur_per_kwh=price))

        return sorted(points, key=lambda p: p.timestamp)

    def _zero_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        now = self._now_func()
        total_hours = max(1, int(horizon.total_seconds() / 3600))
        return [
            TariffPoint(timestamp=now + timedelta(hours=i), price_eur_per_kwh=0.0)
            for i in range(total_hours)
        ]

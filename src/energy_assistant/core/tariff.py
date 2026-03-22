"""TariffModel protocol — describes the pricing structure of an energy flow."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from .models import TariffPoint


@runtime_checkable
class TariffModel(Protocol):
    """Describes the pricing structure of the energy flowing through a device.

    Implementations can be static (flat rate, ToU schedule) or dynamic
    (live feed from Tibber, aWATTar, etc.).
    """

    @property
    def tariff_id(self) -> str:
        """Stable identifier for this tariff, e.g. ``"tibber"`` or ``"export"``."""
        ...

    async def price_at(self, dt: datetime) -> float:
        """Return the price in EUR/kWh at the given moment."""
        ...

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Return hourly import-price points covering approximately *horizon*.

        Points are ordered by timestamp, ascending.
        """
        ...

    async def export_price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Return hourly export (feed-in) price points covering approximately *horizon*.

        Implementations that are import-only should return a schedule of 0.0.
        Points are ordered by timestamp, ascending.
        """
        ...

"""
TariffModel protocol.

Describes the pricing structure of a grid connection or metered circuit.
Multiple tariffs can coexist — for example a dynamic spot-price tariff for
general consumption and a separate flat-rate heat-pump tariff ("Wärmepumpe"
circuit).  Each device declares which tariff applies to it via
``ConfigEntry.tariff_id``.

Implementations can be:
- Static: flat rate, time-of-use schedule
- Dynamic: live feed from Tibber, aWATTar, Awattar, etc.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from .models import TariffPoint


@runtime_checkable
class TariffModel(Protocol):
    @property
    def tariff_id(self) -> str:
        """Stable identifier for this tariff (e.g. ``"hauptstrom"``, ``"waermepumpe"``)."""
        ...

    async def price_at(self, dt: datetime) -> float:
        """Return the grid price in EUR/kWh at *dt*."""
        ...

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """
        Return the price schedule for the next *horizon* duration, ordered by
        timestamp ascending.
        """
        ...

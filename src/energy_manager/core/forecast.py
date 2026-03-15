"""
ForecastProvider protocol.

Multiple providers for the same quantity can coexist; the optimizer selects
or blends them based on configuration.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable

from .models import ForecastPoint, ForecastQuantity


@runtime_checkable
class ForecastProvider(Protocol):
    @property
    def quantity(self) -> ForecastQuantity:
        """The physical quantity this provider forecasts."""
        ...

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """
        Return predicted values for the next *horizon* duration, ordered by
        timestamp ascending.
        """
        ...

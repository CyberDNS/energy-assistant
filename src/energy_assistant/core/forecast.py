"""ForecastProvider protocol — supplies predictions over a planning horizon."""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable

from .models import ForecastPoint, ForecastQuantity


@runtime_checkable
class ForecastProvider(Protocol):
    """Supplies predictions for a scalar quantity over a planning horizon.

    Multiple providers for the same quantity can coexist; the optimizer
    selects or blends them.
    """

    @property
    def quantity(self) -> ForecastQuantity:
        """The physical quantity this provider predicts."""
        ...

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Return forecast points covering approximately *horizon*.

        Points are ordered by timestamp, ascending.
        """
        ...

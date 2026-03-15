"""
Pass-through (zero) forecast plugin.

Returns a flat line of zeros for the requested horizon.  This is the correct
behaviour when no real forecast source is configured: the optimizer runs, but
without any predictive advantage.  It is also the canonical test double for
forecast-dependent code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...core.models import ForecastPoint, ForecastQuantity


class PassThroughForecast:
    """
    A ForecastProvider that always returns zero for every point in the horizon.

    Implements the ``ForecastProvider`` protocol structurally.
    """

    def __init__(self, quantity: ForecastQuantity, interval: timedelta = timedelta(hours=1)) -> None:
        self._quantity = quantity
        self._interval = interval

    @property
    def quantity(self) -> ForecastQuantity:
        return self._quantity

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Return zero-valued points at *interval* spacing covering *horizon*."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        points: list[ForecastPoint] = []
        elapsed = timedelta()
        while elapsed < horizon:
            points.append(ForecastPoint(timestamp=now + elapsed, value=0.0))
            elapsed += self._interval
        return points

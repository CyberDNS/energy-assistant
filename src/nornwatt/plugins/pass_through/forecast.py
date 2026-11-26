"""PassThroughForecast — zero-value stub for missing forecast data.

Used when the real forecast plugin is not configured.  Allows the
optimizer to run on the configured planning horizon with no forecast
data (it will apply the configured baseline load instead).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...core.models import ForecastPoint, ForecastQuantity


class PassThroughForecast:
    """A ``ForecastProvider`` that always returns zeros for every timestep.

    Implements the ``ForecastProvider`` protocol structurally.

    Parameters
    ----------
    quantity:
        The quantity this stub represents (``PRICE``, ``PV_GENERATION``,
        or ``CONSUMPTION``).
    """

    def __init__(self, quantity: ForecastQuantity) -> None:
        self._quantity = quantity

    @property
    def quantity(self) -> ForecastQuantity:
        return self._quantity

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Return hourly zero-value forecast points covering *horizon*."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = int(horizon.total_seconds() / 3600) + 1
        return [
            ForecastPoint(timestamp=now + timedelta(hours=i), value=0.0)
            for i in range(hours)
        ]

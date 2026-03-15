"""
Flat-rate tariff plugin.

The simplest possible tariff: one fixed price at all times.  Useful as a
default for installations that pay a flat per-kWh rate, and as the fallback
tariff in tests and initial setups.

Configuration example (YAML)
----------------------------
    tariffs:
      - id: default
        plugin: energy_manager.plugins.flat_rate
        data:
          price_eur_per_kwh: 0.28

      - id: waermepumpe
        plugin: energy_manager.plugins.flat_rate
        data:
          price_eur_per_kwh: 0.19
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...core.models import TariffPoint


class FlatRateTariff:
    """
    A tariff that charges a single fixed price at all times.

    Implements the ``TariffModel`` protocol structurally.
    """

    def __init__(self, tariff_id: str, price_eur_per_kwh: float) -> None:
        if price_eur_per_kwh < 0:
            raise ValueError("price_eur_per_kwh must be >= 0")
        self._tariff_id = tariff_id
        self._price = price_eur_per_kwh

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    @property
    def price_eur_per_kwh(self) -> float:
        return self._price

    async def price_at(self, dt: datetime) -> float:  # noqa: ARG002
        return self._price

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Return hourly price points for the given horizon, all at the flat rate."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        total_hours = max(1, int(horizon.total_seconds() / 3600))
        return [
            TariffPoint(
                timestamp=now + timedelta(hours=i),
                price_eur_per_kwh=self._price,
            )
            for i in range(total_hours)
        ]

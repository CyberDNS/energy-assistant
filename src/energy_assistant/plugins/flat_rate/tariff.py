"""FlatRateTariff — constant price, no time-of-use variation.

Use cases
---------
- Grid connection point with separate import and export (feed-in) prices.
- Wärmepumpentarif (heat-pump tariff) — a fixed off-peak price granted
  by the utility for heat-pump circuits metered separately (Messkonzept 8).
- Baseline price for testing and simulation.

Two-price grid tariff
---------------------
For a bidirectional meter (e.g. ``main_grid_meter``) supply both parameters:

- ``import_price_eur_per_kwh`` — what you **pay** when drawing from the grid.
- ``export_price_eur_per_kwh`` — what you **receive** when feeding back in.

Both default to ``0.0`` — unused directions cost nothing by default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...core.models import TariffPoint


class FlatRateTariff:
    """A ``TariffModel`` that returns the same price at every moment.

    Implements the ``TariffModel`` protocol structurally (no inheritance).

    Parameters
    ----------
    tariff_id:
        Stable identifier, e.g. ``"grid"`` or ``"heatpump"``.
    import_price_eur_per_kwh:
        Price paid when **importing** from the grid, in EUR/kWh.  Defaults to 0.
    export_price_eur_per_kwh:
        Price received when **exporting** to the grid (feed-in), in EUR/kWh.
        Defaults to 0.
    """

    def __init__(
        self,
        tariff_id: str,
        import_price_eur_per_kwh: float = 0.0,
        export_price_eur_per_kwh: float = 0.0,
    ) -> None:
        self._tariff_id = tariff_id
        self._import_price = import_price_eur_per_kwh
        self._export_price = export_price_eur_per_kwh

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    async def price_at(self, dt: datetime) -> float:
        """Return the **import** price in EUR/kWh."""
        return self._import_price

    async def export_price_at(self, dt: datetime) -> float:
        """Return the **export** (feed-in) price in EUR/kWh."""
        return self._export_price

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        """Return hourly import-price points for the full *horizon*."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = int(horizon.total_seconds() / 3600) + 1
        return [
            TariffPoint(
                timestamp=now + timedelta(hours=i),
                price_eur_per_kwh=self._import_price,
            )
            for i in range(hours)
        ]

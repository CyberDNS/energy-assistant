"""
Tibber direct API — tariff plugin (stub).

Reads spot prices directly from the Tibber GraphQL API without requiring
ioBroker.  Requires a Tibber developer API token.

This module is a placeholder for a future implementation.  Use
``TibberIoBrokerTariff`` from ``energy_manager.plugins.tibber_iobroker``
in the meantime.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ...core.models import TariffPoint


class TibberApiTariff:
    """
    A ``TariffModel`` that reads Tibber prices directly from the Tibber
    GraphQL API.

    Not yet implemented — raises ``NotImplementedError`` on all calls.
    """

    def __init__(self, tariff_id: str, api_token: str) -> None:
        self._tariff_id = tariff_id
        self._api_token = api_token

    @property
    def tariff_id(self) -> str:
        return self._tariff_id

    async def price_at(self, dt: datetime) -> float:
        raise NotImplementedError("TibberApiTariff is not yet implemented")

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        raise NotImplementedError("TibberApiTariff is not yet implemented")

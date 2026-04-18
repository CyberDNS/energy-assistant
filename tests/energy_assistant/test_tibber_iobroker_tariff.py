"""Tests for Tibber ioBroker tariff parsing and current-price lookup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.plugins.tibber_iobroker.tariff import TibberIoBrokerTariff


class _FakeIoBrokerClient:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    async def get_value(self, oid: str):  # noqa: ANN201
        return self._values.get(oid)


@pytest.mark.asyncio
async def test_price_schedule_prefers_total_field_over_energy() -> None:
    client = _FakeIoBrokerClient(
        {
            "tibberlink.0.Homes.home-a.PricesToday.json": [
                {
                    "startsAt": "2026-03-15T00:00:00+01:00",
                    "energy": 0.141,
                    "total": 0.381,
                },
                {
                    "startsAt": "2026-03-15T00:15:00+01:00",
                    "energy": 0.151,
                    "total": 0.391,
                },
            ],
            "tibberlink.0.Homes.home-a.PricesTomorrow.json": [],
        }
    )
    tariff = TibberIoBrokerTariff("household", client, "home-a")

    schedule = await tariff.price_schedule(timedelta(hours=2))

    assert [p.price_eur_per_kwh for p in schedule] == pytest.approx([0.381, 0.391])


@pytest.mark.asyncio
async def test_price_schedule_ignores_entry_without_total() -> None:
    client = _FakeIoBrokerClient(
        {
            "tibberlink.0.Homes.home-a.PricesToday.json": [
                {
                    "startsAt": "2026-03-15T00:00:00+01:00",
                    "energy": 0.281,
                }
            ],
            "tibberlink.0.Homes.home-a.PricesTomorrow.json": [],
        }
    )
    tariff = TibberIoBrokerTariff("household", client, "home-a")

    schedule = await tariff.price_schedule(timedelta(hours=1))

    # No valid total values -> plugin returns zero fallback schedule.
    assert schedule
    assert all(p.price_eur_per_kwh == pytest.approx(0.0) for p in schedule)


@pytest.mark.asyncio
async def test_price_at_prefers_current_total_oid() -> None:
    now = datetime.now(timezone.utc)
    client = _FakeIoBrokerClient(
        {
            "tibberlink.0.Homes.home-a.CurrentPrice.total": 0.4444,
            "tibberlink.0.Homes.home-a.CurrentPrice.energy": 0.1337,
            "tibberlink.0.Homes.home-a.PricesToday.json": [],
            "tibberlink.0.Homes.home-a.PricesTomorrow.json": [],
        }
    )
    tariff = TibberIoBrokerTariff("household", client, "home-a")

    price = await tariff.price_at(now)

    assert price == pytest.approx(0.4444)

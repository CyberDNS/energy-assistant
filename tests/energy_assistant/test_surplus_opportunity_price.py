"""PV opportunity price with separately billed loads (issue #78)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from energy_assistant.core.control import LiveSituation, surplus_opportunity_price
from energy_assistant.core.models import DeviceState

EXPORT = 0.082
HEATPUMP = 0.178


def _price(m: float, z: float, b: float) -> float:
    return surplus_opportunity_price(
        site_grid_w=m, zone_grid_w=z, storage_charge_w=b,
        export_price=EXPORT, secondary_price=HEATPUMP,
    )


def test_charge_the_heat_pump_would_have_used_costs_its_tariff() -> None:
    # Household exports 2.4 kW, battery takes 1.2 kW, main meter imports 2.5 kW.
    assert _price(m=2500.0, z=-2400.0, b=1200.0) == pytest.approx(HEATPUMP)


def test_charge_is_split_where_the_site_would_have_exported() -> None:
    # Main meter imports only 0.5 kW: without the battery 0.5 kW would have
    # gone to the heat pump, the other 0.7 kW to the grid.
    expected = (500.0 * HEATPUMP + 700.0 * EXPORT) / 1200.0
    assert _price(m=500.0, z=-2400.0, b=1200.0) == pytest.approx(expected)


def test_site_exporting_keeps_the_export_price() -> None:
    assert _price(m=-300.0, z=-2400.0, b=1200.0) == pytest.approx(EXPORT)


def test_zone_import_is_not_counted_as_heat_pump_supply() -> None:
    # Household imports 0.5 kW while the battery charges 1 kW and no heat
    # pump runs: of the 1 kW, 0.5 kW would have offset household import and
    # 0.5 kW would have left the site.
    assert _price(m=500.0, z=500.0, b=1000.0) == pytest.approx(EXPORT)


@pytest.mark.parametrize(
    ("m", "z", "expected"),
    [
        (2000.0, -1000.0, HEATPUMP),   # surplus reaches the heat pump
        (-500.0, -1000.0, EXPORT),     # surplus leaves the site
        (2000.0, 300.0, EXPORT),       # no surplus: unchanged behaviour
    ],
)
def test_marginal_price_without_charging(m: float, z: float, expected: float) -> None:
    assert _price(m=m, z=z, b=0.0) == pytest.approx(expected)


def _live(secondary: float | None, zone_w: float | None) -> LiveSituation:
    return LiveSituation(
        timestamp=datetime(2026, 11, 18, 11, 0, tzinfo=UTC),
        grid_power_w=2500.0,
        dt_hours=1 / 360,
        device_states={"bat": DeviceState(device_id="bat", power_w=1200.0)},
        current_price_eur_per_kwh=0.30,
        pv_opportunity_price_eur_per_kwh=EXPORT,
        pv_power_w=4000.0,
        storage_cost_bases={"bat": 0.10},
        default_zone_grid_power_w=zone_w,
        secondary_price_eur_per_kwh=secondary,
    )


def test_ledger_price_uses_the_heat_pump_tariff_for_pv_charging() -> None:
    assert _live(HEATPUMP, -2400.0).market_price_eur_per_kwh == pytest.approx(HEATPUMP)


def test_without_secondary_loads_pv_keeps_the_export_price() -> None:
    assert _live(None, -2400.0).market_price_eur_per_kwh == pytest.approx(EXPORT)
    # No default-zone meter: the split cannot be measured.
    live = _live(HEATPUMP, None)
    live.grid_power_w = -2400.0
    assert live.market_price_eur_per_kwh == pytest.approx(EXPORT)

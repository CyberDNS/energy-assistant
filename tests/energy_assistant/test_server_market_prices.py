"""Tests for tariff-zone market price blending in the server."""

from __future__ import annotations

import pytest

from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import DeviceState
from energy_assistant.server import Application, _TariffZone


def _app_with_zones(zones: dict[str, _TariffZone], pv_opportunity_price: float) -> Application:
    app = Application.__new__(Application)
    app._tariff_zones = zones
    app._pv_opportunity_price = pv_opportunity_price
    app._ledger = BatteryCostLedger()
    return app


def test_differential_feedback_is_clamped_to_diff_load() -> None:
    """Heatpump zone blend must stay convex even when Z1 is exporting.

    Scenario: Z1 exports slightly while Z2 exports more strongly. The
    differential load (Z1 - Z2) is positive, but only that load can be supplied
    by Z2 feedback. Any surplus beyond diff still leaves the site and must not
    enter the blend weight.
    """
    zones = {
        "household": _TariffZone(
            tariff_id="household",
            meter_ids=["household_meter"],
            producer_ids=["pv_production"],
        ),
        "heatpump": _TariffZone(
            tariff_id="heatpump",
            diff_minuend_id="main_grid_meter",
            diff_subtrahend_id="household_meter",
        ),
    }
    app = _app_with_zones(zones, pv_opportunity_price=0.082)

    tariff_prices = {
        "household": 0.35,
        "heatpump": 0.178,
    }
    device_states = {
        "main_grid_meter": DeviceState(device_id="main_grid_meter", power_w=-100.0),
        "household_meter": DeviceState(device_id="household_meter", power_w=-500.0),
        "pv_production": DeviceState(device_id="pv_production", power_w=-600.0),
    }

    prices = app._compute_zone_market_prices(tariff_prices, device_states)

    # With Z1 exporting, none of the differential load is served by direct grid.
    # Heatpump market price should therefore equal the feedback source price,
    # and never exceed import/feedback extrema.
    hp_price = prices["heatpump"]
    assert hp_price == pytest.approx(0.082)
    assert 0.082 <= hp_price <= 0.178


def test_market_breakdown_is_per_zone_not_sitewide() -> None:
    """Household and heatpump zones can have very different source mixes."""
    zones = {
        "household": _TariffZone(
            tariff_id="household",
            meter_ids=["household_meter"],
            storage_ids=["bat"],
        ),
        "heatpump": _TariffZone(
            tariff_id="heatpump",
            diff_minuend_id="main_grid_meter",
            diff_subtrahend_id="household_meter",
        ),
    }
    app = _app_with_zones(zones, pv_opportunity_price=0.082)
    app._ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.10)

    tariff_prices = {
        "household": 0.35,
        "heatpump": 0.178,
    }
    device_states = {
        "main_grid_meter": DeviceState(device_id="main_grid_meter", power_w=2000.0),
        "household_meter": DeviceState(device_id="household_meter", power_w=20.0),
        "bat": DeviceState(device_id="bat", power_w=-1980.0),
    }

    breakdown = app._compute_zone_market_breakdown(tariff_prices, device_states)

    hh = breakdown["household"]
    hp = breakdown["heatpump"]

    # Household is ~1% grid + ~99% battery.
    assert hh["grid_frac"] == pytest.approx(0.01, rel=1e-3)
    assert hh["bat_frac"] == pytest.approx(0.99, rel=1e-3)
    assert hh["price_eur_per_kwh"] == pytest.approx(0.1025, rel=1e-3)

    # Heatpump gets no Z2 feedback here (Z2 imports), so it is 100% grid.
    assert hp["grid_frac"] == pytest.approx(1.0)
    assert hp["pv_frac"] == pytest.approx(0.0)
    assert hp["bat_frac"] == pytest.approx(0.0)
    assert hp["price_eur_per_kwh"] == pytest.approx(0.178)

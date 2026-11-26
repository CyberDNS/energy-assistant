"""Tests for tariff-zone market price blending in the server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nornwatt.core.device_model import DeviceCategory, DeviceSpec
from nornwatt.core.ledger import BatteryCostLedger
from nornwatt.core.models import DeviceState
from nornwatt.server import Application, _TariffZone


class _Registry:
    def __init__(self, categories: dict[str, DeviceCategory]) -> None:
        self._categories = categories

    def get(self, device_id: str):
        category = self._categories.get(device_id)
        return None if category is None else SimpleNamespace(device_id=device_id, category=category)


def _app_with_zones(
    zones: dict[str, _TariffZone],
    pv_opportunity_price: float,
    devices: dict[str, DeviceSpec] | None = None,
    categories: dict[str, DeviceCategory] | None = None,
) -> Application:
    app = Application.__new__(Application)
    app._tariff_zones = zones
    app._pv_opportunity_price = pv_opportunity_price
    app._ledger = BatteryCostLedger()
    app._cfg = SimpleNamespace(default_tariff_id="household", devices=devices or {})
    app._registry = _Registry(categories or {})
    app._tariffs = {tid: object() for tid in ("household", "heatpump")}
    app._grid_meter_id = "main_grid_meter"
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


def _mk8_app() -> Application:
    zones = {
        "household": _TariffZone(
            tariff_id="household",
            meter_ids=["household_meter"],
            producer_ids=["pv_production"],
            storage_ids=["bat"],
        ),
        "heatpump": _TariffZone(
            tariff_id="heatpump",
            diff_minuend_id="main_grid_meter",
            diff_subtrahend_id="household_meter",
        ),
    }
    return _app_with_zones(
        zones,
        pv_opportunity_price=0.082,
        devices={
            "heatpump": DeviceSpec(id="heatpump", category=DeviceCategory.CONSUMER,
                                   type="differential", tariff="heatpump"),
            "household_meter": DeviceSpec(id="household_meter", category=DeviceCategory.METER,
                                          type="tibber_local", tariff="household"),
        },
        categories={"heatpump": DeviceCategory.CONSUMER, "household_meter": DeviceCategory.METER},
    )


def test_household_pv_feeding_heatpump_is_valued_at_heatpump_tariff() -> None:
    """Issue #78: household surplus the heat pump would use is not worth export.

    The household meter exports 2.4 kW while the battery charges 1.2 kW, and
    the main meter imports 2.5 kW for the heat pump.  Without the battery the
    heat pump would have used those 1.2 kW, so PV in the household zone is
    worth the heat pump tariff.
    """
    app = _mk8_app()
    device_states = {
        "main_grid_meter": DeviceState(device_id="main_grid_meter", power_w=2500.0),
        "household_meter": DeviceState(device_id="household_meter", power_w=-2400.0),
        "pv_production": DeviceState(device_id="pv_production", power_w=-4000.0),
        "bat": DeviceState(device_id="bat", power_w=1200.0),
        "heatpump": DeviceState(device_id="heatpump", power_w=2500.0),
    }
    hh = app._compute_zone_market_breakdown(
        {"household": 0.35, "heatpump": 0.178}, device_states
    )["household"]
    assert hh["pv_frac"] == pytest.approx(1.0)
    assert hh["price_eur_per_kwh"] == pytest.approx(0.178)


def test_household_pv_keeps_export_price_when_site_exports() -> None:
    app = _mk8_app()
    device_states = {
        "main_grid_meter": DeviceState(device_id="main_grid_meter", power_w=-1500.0),
        "household_meter": DeviceState(device_id="household_meter", power_w=-2400.0),
        "pv_production": DeviceState(device_id="pv_production", power_w=-4000.0),
        "bat": DeviceState(device_id="bat", power_w=1200.0),
        "heatpump": DeviceState(device_id="heatpump", power_w=900.0),
    }
    hh = app._compute_zone_market_breakdown(
        {"household": 0.35, "heatpump": 0.178}, device_states
    )["household"]
    assert hh["price_eur_per_kwh"] == pytest.approx(0.082)

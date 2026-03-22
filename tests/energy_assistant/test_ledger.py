"""Tests for BatteryCostLedger."""

from __future__ import annotations

import pytest

from energy_assistant.core.ledger import BatteryCostLedger


class TestBatteryCostLedgerInitialise:
    def test_cost_basis_after_initialise(self) -> None:
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.22)
        assert ledger.cost_basis("bat") == pytest.approx(0.22)
        assert ledger.stored_energy("bat") == pytest.approx(5.0)

    def test_unknown_device_returns_none(self) -> None:
        ledger = BatteryCostLedger()
        assert ledger.cost_basis("unknown") is None
        assert ledger.stored_energy("unknown") is None


class TestBatteryCostLedgerCharge:
    def test_charge_raises_basis_when_expensive(self) -> None:
        """Charging at a higher price than the current basis raises the basis."""
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.20)
        # Charge 2 kWh grid energy @ 0.30 €/kWh, η=1.0 for easy arithmetic
        ledger.record_charge("bat", delta_kwh=2.0, price_eur_per_kwh=0.30,
                             charge_efficiency=1.0)
        # new_basis = (5×0.20 + 2×0.30) / (5+2) = (1.00+0.60)/7 = 0.2286
        assert ledger.cost_basis("bat") == pytest.approx(1.60 / 7.0, rel=1e-5)
        assert ledger.stored_energy("bat") == pytest.approx(7.0)

    def test_charge_lowers_basis_when_cheap(self) -> None:
        """Charging at a lower price than the current basis lowers the basis."""
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.30)
        ledger.record_charge("bat", delta_kwh=5.0, price_eur_per_kwh=0.10,
                             charge_efficiency=1.0)
        # new_basis = (5×0.30 + 5×0.10) / 10 = 2.0/10 = 0.20
        assert ledger.cost_basis("bat") == pytest.approx(0.20, rel=1e-5)

    def test_charge_efficiency_increases_effective_cost(self) -> None:
        """With η_c=0.9, the effective cost per stored kWh = price / η_c."""
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=0.0, cost_basis_eur_per_kwh=0.0)
        # charge 1 kWh AC @ 0.18 €/kWh, η=0.9 → 0.9 kWh stored @ 0.20 €/kWh
        ledger.record_charge("bat", delta_kwh=1.0, price_eur_per_kwh=0.18,
                             charge_efficiency=0.90)
        assert ledger.stored_energy("bat") == pytest.approx(0.9, rel=1e-5)
        assert ledger.cost_basis("bat") == pytest.approx(0.18 / 0.90, rel=1e-5)

    def test_zero_delta_is_ignored(self) -> None:
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.25)
        ledger.record_charge("bat", delta_kwh=0.0, price_eur_per_kwh=0.50)
        assert ledger.cost_basis("bat") == pytest.approx(0.25)


class TestBatteryCostLedgerDischarge:
    def test_discharge_reduces_stored_energy(self) -> None:
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=8.0, cost_basis_eur_per_kwh=0.22)
        ledger.record_discharge("bat", delta_kwh=3.0)
        assert ledger.stored_energy("bat") == pytest.approx(5.0)

    def test_discharge_does_not_change_basis(self) -> None:
        """Average-cost method: remaining energy still costs the same."""
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=10.0, cost_basis_eur_per_kwh=0.22)
        ledger.record_discharge("bat", delta_kwh=4.0)
        assert ledger.cost_basis("bat") == pytest.approx(0.22)

    def test_discharge_cannot_go_below_zero(self) -> None:
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=2.0, cost_basis_eur_per_kwh=0.22)
        ledger.record_discharge("bat", delta_kwh=5.0)
        assert ledger.stored_energy("bat") == pytest.approx(0.0)


class TestBatteryCostLedgerSpotFloor:
    def test_spot_floor_resets_when_cheaper(self) -> None:
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.30)
        ledger.apply_spot_floor("bat", spot_price=0.18)
        assert ledger.cost_basis("bat") == pytest.approx(0.18)

    def test_spot_floor_does_not_raise_basis(self) -> None:
        """If spot > current basis, basis must not increase."""
        ledger = BatteryCostLedger()
        ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.15)
        ledger.apply_spot_floor("bat", spot_price=0.30)
        assert ledger.cost_basis("bat") == pytest.approx(0.15)


class TestBatteryCostLedgerAllCostBases:
    def test_returns_all_devices(self) -> None:
        ledger = BatteryCostLedger()
        ledger.initialise("bat1", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.20)
        ledger.initialise("bat2", stored_energy_kwh=3.0, cost_basis_eur_per_kwh=0.25)
        bases = ledger.all_cost_bases()
        assert set(bases.keys()) == {"bat1", "bat2"}
        assert bases["bat1"] == pytest.approx(0.20)
        assert bases["bat2"] == pytest.approx(0.25)

"""BatteryCostLedger — tracks the weighted-average cost basis of stored energy.

Concept
-------
Every kWh currently sitting in a battery was charged at some price.  The
cost basis is the weighted average of those prices, adjusted for round-trip
efficiency losses.  It answers the question:

    "What did I effectively pay per kWh of *usable* energy currently stored?"

The optimizer uses the cost basis as a **terminal value**: energy still in the
battery at the end of the planning horizon is worth ``cost_basis`` €/kWh, so
the solver will never discharge it at a price lower than that.

Update rules
------------
**Charging** (adding energy at price ``p`` €/kWh, efficiency η_c):

    new_basis = (old_energy × old_basis + charge_kwh × p / η_c) / new_energy

The cost per usable stored kWh increases by the charging inefficiency: you
pay for 1 kWh from the grid but only η_c kWh ends up stored.

**Discharging** (removing energy; the cheapest kWh leave first):

    Cost basis is unchanged — the remaining energy still cost what it cost.
    (FIFO or average-cost — they are equivalent for a single cost basis.)

**Reset-to-spot floor**:

    If the current best available charge price ``spot`` < cost_basis, then:
        cost_basis = max(cost_basis - decay_rate × Δt, spot)

    Rationale: you can always recharge at ``spot`` right now, so holding
    energy that cost more than ``spot`` has an opportunity cost.  Over time
    the basis decays toward the current spot price if the spot is cheaper.

Usage
-----
::

    ledger = BatteryCostLedger()
    ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.22)

    # When the battery charges:
    ledger.record_charge("bat", delta_kwh=1.0, price_eur_per_kwh=0.18,
                         charge_efficiency=0.95)

    # When the battery discharges:
    ledger.record_discharge("bat", delta_kwh=0.5)

    # Apply spot-price floor (call periodically):
    ledger.apply_spot_floor("bat", spot_price=0.16)

    # Read current cost basis (used by optimizer as terminal value):
    basis = ledger.cost_basis("bat")   # → 0.xxx €/kWh
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

# Below this stored energy the cost basis is considered unreliable and is
# reset to the last seen charge price on the next charge event.
_MIN_ENERGY_KWH = 0.1


@dataclass
class _BatteryEntry:
    stored_energy_kwh: float   # kWh currently stored (usable, after η_c applied)
    cost_basis: float          # €/kWh weighted average


class BatteryCostLedger:
    """In-memory weighted-average cost basis tracker for storage devices.

    Thread-safety: single-threaded async use only (no locks).
    """

    def __init__(self) -> None:
        self._entries: dict[str, _BatteryEntry] = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialise(
        self,
        device_id: str,
        stored_energy_kwh: float,
        cost_basis_eur_per_kwh: float,
    ) -> None:
        """Set the initial state for a device.

        Call once on startup (or after a restart) before the first
        ``record_charge`` / ``record_discharge``.
        """
        self._entries[device_id] = _BatteryEntry(
            stored_energy_kwh=max(0.0, stored_energy_kwh),
            cost_basis=cost_basis_eur_per_kwh,
        )
        _log.debug(
            "BatteryCostLedger: initialised %r  stored=%.2f kWh  basis=%.4f €/kWh",
            device_id,
            stored_energy_kwh,
            cost_basis_eur_per_kwh,
        )

    # ------------------------------------------------------------------
    # Update events
    # ------------------------------------------------------------------

    def record_charge(
        self,
        device_id: str,
        delta_kwh: float,
        price_eur_per_kwh: float,
        charge_efficiency: float = 0.95,
    ) -> None:
        """Update cost basis after charging ``delta_kwh`` kWh from the grid.

        Parameters
        ----------
        delta_kwh:
            AC energy drawn from grid (before efficiency losses), kWh.
        price_eur_per_kwh:
            Import price paid for this energy.
        charge_efficiency:
            Fraction of AC energy that ends up stored (default 0.95).
        """
        if delta_kwh <= 0:
            return
        entry = self._get_or_create(device_id, price_eur_per_kwh)

        # kWh actually added to storage after efficiency loss
        stored_delta = delta_kwh * charge_efficiency
        # effective cost per stored kWh = import price / efficiency
        cost_per_stored = price_eur_per_kwh / charge_efficiency

        old_total = entry.stored_energy_kwh * entry.cost_basis
        new_energy = entry.stored_energy_kwh + stored_delta
        entry.cost_basis = (old_total + stored_delta * cost_per_stored) / new_energy
        entry.stored_energy_kwh = new_energy

        _log.debug(
            "BatteryCostLedger: %r charged %.3f kWh @ %.4f €/kWh  "
            "→ stored=%.2f kWh  basis=%.4f €/kWh",
            device_id, delta_kwh, price_eur_per_kwh,
            entry.stored_energy_kwh, entry.cost_basis,
        )

    def record_discharge(
        self,
        device_id: str,
        delta_kwh: float,
    ) -> None:
        """Update stored energy after discharging ``delta_kwh`` kWh.

        The cost basis of the *remaining* energy is unchanged (average-cost
        method: all stored kWh are considered equally valued).

        Parameters
        ----------
        delta_kwh:
            Stored energy removed (DC side, before discharge efficiency),
            kWh.  Must be positive.
        """
        if delta_kwh <= 0:
            return
        entry = self._get_or_create(device_id, 0.0)
        entry.stored_energy_kwh = max(0.0, entry.stored_energy_kwh - delta_kwh)

        _log.debug(
            "BatteryCostLedger: %r discharged %.3f kWh  → stored=%.2f kWh  basis=%.4f €/kWh",
            device_id, delta_kwh, entry.stored_energy_kwh, entry.cost_basis,
        )

    def apply_spot_floor(
        self,
        device_id: str,
        spot_price: float,
    ) -> None:
        """Decay cost basis toward ``spot_price`` if spot is cheaper.

        If the current best charge price is lower than the cost basis,
        the opportunity cost of holding the stored energy falls — you could
        discharge, take the break-even loss, and refill cheaper.  This call
        immediately resets the basis to ``spot_price`` in that case.

        Call this periodically (e.g. on every new price tick) to keep the
        basis grounded in current market conditions.
        """
        entry = self._get_or_create(device_id, spot_price)
        if spot_price < entry.cost_basis:
            _log.debug(
                "BatteryCostLedger: %r spot floor applied  "
                "basis %.4f → %.4f €/kWh",
                device_id, entry.cost_basis, spot_price,
            )
            entry.cost_basis = spot_price

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def cost_basis(self, device_id: str) -> float | None:
        """Return current cost basis in €/kWh, or ``None`` if unknown."""
        entry = self._entries.get(device_id)
        return entry.cost_basis if entry is not None else None

    def stored_energy(self, device_id: str) -> float | None:
        """Return currently tracked stored energy in kWh, or ``None`` if unknown."""
        entry = self._entries.get(device_id)
        return entry.stored_energy_kwh if entry is not None else None

    def all_cost_bases(self) -> dict[str, float]:
        """Return cost basis for every tracked device."""
        return {did: e.cost_basis for did, e in self._entries.items()}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, device_id: str, default_basis: float) -> _BatteryEntry:
        if device_id not in self._entries:
            _log.warning(
                "BatteryCostLedger: %r not initialised — creating with basis %.4f €/kWh",
                device_id, default_basis,
            )
            self._entries[device_id] = _BatteryEntry(
                stored_energy_kwh=0.0,
                cost_basis=default_basis,
            )
        return self._entries[device_id]

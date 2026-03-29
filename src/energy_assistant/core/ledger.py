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
        cost_basis decays exponentially toward spot at rate:
            k = max_charge_kw × η_c / stored_energy_kwh  (h⁻¹)
        so: cost_basis(t) = spot + (cost_basis₀ − spot) × exp(−k × t)

    Intuition: you can always recharge at ``spot`` right now.  The faster
    you can refill the battery (high max_charge_kw relative to stored energy),
    the faster the basis tracks the current spot price.  When timing
    information is unavailable, the basis is snapped to spot immediately
    (legacy / fallback behaviour).

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
import math
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
        dt_hours: float = 0.0,
        max_charge_kw: float = 0.0,
        charge_efficiency: float = 0.95,
    ) -> None:
        """Decay cost basis toward ``spot_price`` when spot is cheaper.

        Gradual decay (preferred)
        ~~~~~~~~~~~~~~~~~~~~~~~~~
        When ``dt_hours > 0`` and ``max_charge_kw > 0``, the basis decays
        exponentially toward ``spot_price`` at a rate determined by how
        quickly the battery *could* be refilled at the current spot price:

            rate = max_charge_kw × η_c / stored_energy_kwh   (h⁻¹)
            new_basis = spot + (basis − spot) × exp(−rate × dt_hours)

        Intuition: if you *could* replace all stored energy in 1 hour at
        current spot, the cost basis follows spot within an hour.  If the
        battery is large relative to the charger it takes proportionally
        longer.  The basis never drops below ``spot_price``.

        Instant reset (legacy / fallback)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        When ``dt_hours == 0`` or ``max_charge_kw == 0`` the basis is
        snapped immediately to ``spot_price`` when spot is cheaper.  This
        preserves backward-compatible behaviour for callers that do not
        supply timing information.

        Call this periodically (e.g. on every new price tick or control
        tick) to keep the basis grounded in current market conditions.
        """
        entry = self._get_or_create(device_id, spot_price)
        if spot_price >= entry.cost_basis:
            return  # spot is not cheaper — no decay needed

        if dt_hours > 0.0 and max_charge_kw > 0.0:
            stored = max(entry.stored_energy_kwh, _MIN_ENERGY_KWH)
            rate = max_charge_kw * charge_efficiency / stored  # h⁻¹
            new_basis = spot_price + (entry.cost_basis - spot_price) * math.exp(-rate * dt_hours)
            new_basis = max(new_basis, spot_price)
            _log.debug(
                "BatteryCostLedger: %r spot decay  basis %.4f → %.4f €/kWh"
                "  (rate=%.3f h⁻¹  dt=%.4f h)",
                device_id, entry.cost_basis, new_basis, rate, dt_hours,
            )
            entry.cost_basis = new_basis
        else:
            # Instant snap — backward-compatible fallback
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

    def set_stored_energy(self, device_id: str, stored_energy_kwh: float) -> None:
        """Set stored energy directly (typically from live SoC).

        Keeps the existing cost basis unchanged. If the device is unknown,
        creates an entry with zero basis as a safe fallback.
        """
        entry = self._entries.get(device_id)
        if entry is None:
            self._entries[device_id] = _BatteryEntry(
                stored_energy_kwh=max(0.0, stored_energy_kwh),
                cost_basis=0.0,
            )
            return
        entry.stored_energy_kwh = max(0.0, stored_energy_kwh)

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

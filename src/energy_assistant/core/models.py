"""
Pure data models for the energy management platform.

All models use Pydantic v2 for validation and serialisation.
No I/O or imports from other application modules live here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DeviceRole(str, Enum):
    """Semantic label describing what a device fundamentally *is* in the energy system."""

    METER = "meter"
    PRODUCER = "producer"
    STORAGE = "storage"
    CONSUMER = "consumer"
    EV_CHARGER = "ev_charger"


def parse_device_role(
    raw: str | None,
    default: DeviceRole = DeviceRole.CONSUMER,
) -> DeviceRole:
    """Parse *raw* into a ``DeviceRole``, returning *default* on unknown values."""
    try:
        return DeviceRole(raw or "")
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            "Unknown device role %r — defaulting to %s", raw, default.value
        )
        return default


class DeviceState(BaseModel):
    """Normalised snapshot of a device's current readings.

    Sign convention
    ---------------
    ``power_w > 0``  — device is consuming / grid is importing
    ``power_w < 0``  — device is producing / grid is exporting
    """

    device_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    power_w: float | None = None
    """Net power in watts.  Positive = consuming/importing.  Negative = producing/exporting."""

    energy_kwh: float | None = None
    """Cumulative energy counter, kWh (optional — not all devices expose this)."""

    soc_pct: float | None = None
    """State of charge, 0–100.  Only meaningful for storage devices."""

    available: bool = True
    """False when the device is unreachable or returned an error."""

    extra: dict[str, Any] = Field(default_factory=dict)
    """Plugin-specific extras (e.g. ``import_w``, ``export_w`` for bidirectional meters)."""


class DeviceCommand(BaseModel):
    """A command sent to a device via ``Device.send_command``."""

    device_id: str
    command: str
    """E.g. ``"turn_on"``, ``"turn_off"``, ``"set_power_w"``."""
    value: Any = None


class StorageConstraints(BaseModel):
    """Physical limits for a storage device, declared for the MILP optimizer."""

    device_id: str
    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    min_soc_pct: float = 0.0
    max_soc_pct: float = 100.0
    purchase_price_eur: float | None = None
    """Total purchase price of the battery system in EUR (hardware only, excl. installation)."""
    cycle_life: int | None = None
    """Rated full-cycle lifetime of the battery (manufacturer spec, e.g. 3000 cycles at 80% DoD)."""
    no_grid_charge: bool = False
    """When True the battery may only charge from local PV surplus, never from grid import."""

    @property
    def degradation_cost_per_kwh(self) -> float:
        """Amortised degradation cost per kWh *stored* (€/kWh).

        Computed as ``purchase_price_eur / (cycle_life × capacity_kwh)``.
        Returns 0.0 when either field is absent or zero.

        Interpretation: every kWh that passes through the battery costs this
        much in wear.  The MILP adds this to the charge cost so the optimizer
        only cycles the battery when the price spread justifies the wear.
        """
        if self.purchase_price_eur and self.cycle_life and self.capacity_kwh:
            return self.purchase_price_eur / (self.cycle_life * self.capacity_kwh)
        return 0.0


class Measurement(BaseModel):
    """A single time-series data point persisted to the storage backend."""

    device_id: str
    timestamp: datetime
    power_w: float | None = None
    energy_kwh: float | None = None
    soc_pct: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class TariffPoint(BaseModel):
    """A single price point in a tariff schedule."""

    timestamp: datetime
    price_eur_per_kwh: float


class ForecastQuantity(str, Enum):
    """The physical quantity a ForecastProvider predicts."""

    PRICE = "price"
    PV_GENERATION = "pv_generation"
    CONSUMPTION = "consumption"


class ForecastPoint(BaseModel):
    """A single point in a forecast time series."""

    timestamp: datetime
    value: float


class ControlIntent(BaseModel):
    """A single timestep intent within an EnergyPlan.

    Describes *what* a device should do and within *what* power bounds.
    The fast control loop resolves these bounds against live measurements.

    Canonical modes (emitted by the MILP optimizer)
    ------------------------------------------------
    ``charge_from_pv``    Absorb PV surplus only; never increase grid import.
                          ``planned_kw`` is the optimizer's forecast allocation
                          for this battery (0 = absorb whatever arrives).
    ``charge_from_grid``  Charge at ``planned_kw``; grid import is allowed.
    ``discharge``         Reduce import on the ``zone_id`` meter up to
                          ``planned_kw``.  No export permitted.
    ``grid_feed_in``      Actively push stored energy past the site boundary
                          into the grid.  Zone context is irrelevant.

    Deprecated aliases (still accepted by the controller for backward-compat)
    --------------------------------------------------------------------------
    ``idle``       → treated as ``charge_from_pv`` at 0 planned kW.
    ``grid_fill``  → treated as ``charge_from_grid`` (or ``charge_from_pv``
                      when ``charge_policy == "pv_only"`` or device has
                      ``no_grid_charge``).
    """

    device_id: str
    timestep: datetime
    mode: str
    zone_id: str | None = None
    """Zone / meter context for ``discharge`` intents.

    Identifies which sub-meter the optimizer targets when requesting discharge.
    ``None`` in ``charge_from_pv``, ``charge_from_grid``, and ``grid_feed_in``
    intents (zone is irrelevant for those modes).
    """
    min_power_w: float | None = None
    max_power_w: float | None = None
    planned_kw: float | None = None
    """Average power the optimizer planned for this timestep (kW).

    Sign convention mirrors the platform: positive = charging/consuming,
    negative = discharging/generating.  Populated by the MILP optimizer;
    ``None`` for intents produced outside the MILP (e.g. rule-based fallbacks).
    """
    reserved_kwh: float | None = None
    """Energy budget reserved by the optimizer for this timestep (kWh).

    Positive = charge budget allocated; negative = discharge budget allocated.
    The fast control loop uses this to track how much of the planned energy
    has actually been delivered so it can decide whether to allow PV overflow
    on top of a partially-filled slot.
    """

    charge_policy: str = "auto"
    """Charging source policy — informational / backward-compat for legacy modes.

    When ``mode`` is one of the canonical values (``charge_from_pv``,
    ``charge_from_grid``, ``discharge``, ``grid_feed_in``) the mode itself fully
    encodes the charge source and this field is only kept for observability.

    For legacy ``grid_fill`` / ``idle`` intents the controller still reads this:
    - ``auto``          → resolve from device capability (``no_grid_charge`` flag).
    - ``pv_only``       → charge only from live PV surplus.
    - ``grid_allowed``  → allow grid import to meet the planned power.
    - ``grid_only``     → grid source explicit (best-effort; source separation
                          is difficult to enforce in AC-coupled systems).
    """

    discharge_policy: str = "meet_load_only"
    """Discharge/export policy — informational / backward-compat for legacy modes.

    When ``mode`` is one of the canonical values the mode itself encodes export
    intent (``discharge`` = no export; ``grid_feed_in`` = export allowed).

    For legacy ``discharge`` intents the controller still reads this:
    - ``meet_load_only``             → cap to live import demand (no export).
    - ``forbid_export``              → identical to ``meet_load_only``.
    - ``allow_export_if_profitable`` → export when battery basis ≤ export price.
    - ``auto``                       → treated as ``meet_load_only`` for safety.
    """


class EnergyPlan(BaseModel):
    """Time-indexed schedule of control intents for all controllable devices.

    Produced by the Optimizer and consumed by the fast control loop.
    """

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    horizon_hours: int = 24
    intents: list[ControlIntent] = Field(default_factory=list)


class ConfigEntry(BaseModel):
    """A single device/plugin declaration from the config manager.

    ``data`` is a free-form dict validated by the plugin that owns the entry.
    """

    id: str
    plugin: str
    role: DeviceRole | None = None
    tariff_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

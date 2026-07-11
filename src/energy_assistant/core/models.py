"""
Pure data models for the energy management platform.

All models use Pydantic v2 for validation and serialisation.
No I/O or imports from other application modules live here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from typing import Literal

from pydantic import BaseModel, Field


class DeviceRole(str, Enum):
    """Semantic label describing what a device fundamentally *is* in the energy system."""

    METER = "meter"
    PRODUCER = "producer"
    STORAGE = "storage"
    CONSUMER = "consumer"
    EV_CHARGER = "ev_charger"
    THRESHOLD_CONTROLLED = "threshold_controlled"


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


class ThresholdConstraints(BaseModel):
    """Physical limits for a threshold-controlled device, declared for the MILP optimizer.

    A threshold device is any load that keeps a measured environmental value
    (temperature, humidity, CO₂, …) between ``bottom_threshold`` and
    ``top_threshold``.  Examples: aquarium cooler, dehumidifier, space heater.

    The MILP models the device as a binary on/off variable and tracks the
    value trajectory over the horizon, scheduling runtime during cheap hours
    while guaranteeing the value stays within bounds.

    Direction semantics
    -------------------
    ``"reduces"``  — device reduces the value when running and the value drifts
                     *upward* when off (cooler, dehumidifier).
    ``"increases"`` — device increases the value when running and the value
                      drifts *downward* when off (heater, humidifier).

    The current measured value must be supplied as
    ``DeviceState.extra["measured_value"]`` for the optimizer to use as the
    initial condition.
    """

    device_id: str
    bottom_threshold: float
    """Lower bound of the allowed range (e.g. 24 °C, 40 % RH)."""
    top_threshold: float
    """Upper bound of the allowed range (e.g. 28 °C, 70 % RH)."""
    unit: str = ""
    """Display unit — informational only (e.g. '°C', '%RH')."""
    direction: Literal["reduces", "increases"] = "reduces"
    rated_power_kw: float
    """Electrical power drawn when the device is running (kW)."""
    active_rate_per_h: float
    """Absolute rate of change while running (value units per hour).

    E.g. a cooler that drops 2 °C/h → ``active_rate_per_h = 2.0``.
    """
    drift_rate_per_h: float
    """Absolute rate of natural drift when the device is off (value units per hour).

    E.g. an aquarium warming 1 °C/h when the cooler is off → ``drift_rate_per_h = 1.0``.
    """
    min_runtime_h: float = 0.0
    """Minimum continuous runtime once started (compressor protection), in hours."""
    min_offtime_h: float = 0.0
    """Minimum off time after stopping (compressor protection), in hours."""
    label: str = ""
    """Human-readable display name shown in the UI (defaults to device_id if empty)."""


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

    Two orthogonal fields fully describe what a device should do:

    ``power_kw``       Signed planned power (kW).
                       Positive  = charge / consume (battery charging, EV, threshold device on).
                       Negative  = discharge / produce (battery discharging).
                       Zero      = idle; for storage this means "absorb whatever PV surplus
                                   arrives" rather than "do nothing".
    ``grid_allowed``   True  → grid import is permitted to meet ``power_kw``.
                       False → charging is capped to live PV surplus only.
    ``export_allowed`` True  → device may actively push power past the site boundary
                               into the grid (battery feed-in).
                       False → discharge is capped to live site import (no export).
    """

    device_id: str
    timestep: datetime
    power_kw: float
    """Signed planned power (kW). + = charge/consume, − = discharge/produce."""

    grid_allowed: bool = True
    """False → PV surplus only; no grid import for charging."""

    export_allowed: bool = False
    """True → may actively export past the site boundary into the grid."""

    zone_id: str | None = None
    reserved_kwh: float | None = None
    """Energy budget reserved by the optimizer for this timestep (kWh)."""

    stored_energy_kwh: float | None = None
    """Stored energy in the battery at the END of this timestep (kWh).

    Populated by the MILP optimizer from the e[b,t] variable. Used by the
    UI to display the SoC trajectory chart.
    """


def intent_display_mode(intent: ControlIntent, is_threshold: bool = False) -> str:
    """Derive a human-readable mode string from an intent's numeric fields.

    Used by the API layer to produce backward-compatible JSON for the frontend.
    """
    if is_threshold:
        return "run" if intent.power_kw > 0.001 else "standby"
    if intent.power_kw > 0.001:
        return "charge_from_grid" if intent.grid_allowed else "charge_from_pv"
    if intent.power_kw < -0.001:
        return "grid_feed_in" if intent.export_allowed else "discharge"
    return "idle"


class PlanFlow(BaseModel):
    """Solved site-level energy flows for one plan timestep.

    Carries the values the optimizer actually solved with — including
    internal adjustments like the live-PV floor for the current hour —
    so the UI can display grid import/export consistent with the plan
    instead of re-deriving them from the raw forecast series.
    """

    timestep: datetime
    pv_kw: float
    """Effective PV used by the solver (forecast, possibly live-floored)."""
    grid_import_kw: float
    grid_export_kw: float


class EnergyPlan(BaseModel):
    """Time-indexed schedule of control intents for all controllable devices.

    Produced by the Optimizer and consumed by the fast control loop.
    """

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    horizon_hours: int = 24
    step_minutes: int = 15
    """Slot length of the plan grid.  An intent is active from its timestep
    for exactly one step — critical for sparse intent sets (EVs only get
    intents for charging slots), where the most-recent-≤-now lookup would
    otherwise keep a stale intent active until the end of the plan."""
    intents: list[ControlIntent] = Field(default_factory=list)
    flows: list[PlanFlow] = Field(default_factory=list)
    """Solved per-timestep site flows (empty for plans without a solve)."""


class ConfigEntry(BaseModel):
    """A single device/plugin declaration from the config manager.

    ``data`` is a free-form dict validated by the plugin that owns the entry.
    """

    id: str
    plugin: str
    role: DeviceRole | None = None
    tariff_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

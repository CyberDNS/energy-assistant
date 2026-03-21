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

    Charge modes
    ------------
    ``idle``          Do nothing, send no command.
    ``pv_overflow``   Track live PV surplus; stay within [min_power_w, max_power_w].
    ``grid_fill``     Draw from grid at planned power; stay within bounds.
    ``target_soc``    Distribute remaining energy over remaining time to deadline.
    ``discharge``     Feed stored energy into the home; track live deficit.
    """

    device_id: str
    timestep: datetime
    mode: str
    min_power_w: float | None = None
    max_power_w: float | None = None


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

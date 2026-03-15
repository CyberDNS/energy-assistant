"""
Pure data models shared across all layers of the platform.

All models use Pydantic v2 for validation, serialization, and schema generation.
No I/O or business logic lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Device categories
# ---------------------------------------------------------------------------


class DeviceCategory(str, Enum):
    SOURCE = "source"      # produces energy (solar inverter, wind turbine)
    STORAGE = "storage"    # stores and releases energy (battery)
    CONSUMER = "consumer"  # consumes energy (EV charger, heat pump, appliance)
    METER = "meter"        # measures energy flow (grid meter, sub-meter)


# ---------------------------------------------------------------------------
# Device state & commands
# ---------------------------------------------------------------------------


class DeviceState(BaseModel):
    """Normalised snapshot of a device's readings at a point in time."""

    device_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    # Instantaneous power in watts.  Positive = producing/charging; negative = consuming.
    power_w: float | None = None
    # Cumulative energy counter in kWh (reset policy is device-specific).
    energy_kwh: float | None = None
    # State of charge in percent 0–100 (relevant for STORAGE devices).
    soc_pct: float | None = None
    available: bool = True
    # Catch-all for device-specific fields that don't fit the normalised schema.
    extra: dict[str, Any] = Field(default_factory=dict)


class DeviceCommand(BaseModel):
    """A command to send to a device."""

    device_id: str
    command: str
    value: Any = None


# ---------------------------------------------------------------------------
# Measurements (persisted history)
# ---------------------------------------------------------------------------


class Measurement(BaseModel):
    """A single time-series data point written to the storage backend."""

    device_id: str
    timestamp: datetime
    power_w: float | None = None
    energy_kwh: float | None = None
    soc_pct: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tariff
# ---------------------------------------------------------------------------


class TariffPoint(BaseModel):
    """Grid electricity price at a specific point in time."""

    timestamp: datetime
    price_eur_per_kwh: float


# ---------------------------------------------------------------------------
# Forecasts
# ---------------------------------------------------------------------------


class ForecastQuantity(str, Enum):
    PRICE = "price"
    PV_GENERATION = "pv_generation"
    CONSUMPTION = "consumption"


class ForecastPoint(BaseModel):
    """A single predicted value for a given forecast quantity."""

    timestamp: datetime
    value: float


# ---------------------------------------------------------------------------
# Energy plan (optimizer output)
# ---------------------------------------------------------------------------


class ControlAction(BaseModel):
    """A single scheduled control action inside an EnergyPlan."""

    device_id: str
    command: str
    value: Any = None
    scheduled_at: datetime


class EnergyPlan(BaseModel):
    """Time-indexed schedule of control actions produced by the optimizer."""

    created_at: datetime = Field(default_factory=_utcnow)
    horizon_hours: int = 24
    actions: list[ControlAction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigEntry(BaseModel):
    """
    A single device or integration declaration.

    ``data`` is a freeform dict whose schema is defined and validated by the
    plugin identified by ``plugin``.

    ``tariff_id`` links this device to a specific tariff by its id.  When
    ``None`` the optimizer falls back to the tariff whose id is ``"default"``
    (if one is registered), otherwise no cost calculation is performed for
    this device.
    """

    id: str
    plugin: str
    tariff_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

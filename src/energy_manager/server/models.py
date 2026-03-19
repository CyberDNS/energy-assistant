"""Pydantic response models for the Energy Assistant REST API."""

from __future__ import annotations

from pydantic import BaseModel


class BatteryCard(BaseModel):
    """Snapshot of a single battery device."""

    device_id: str
    soc_pct: float | None
    power_w: float | None
    controllable: bool


class HomePowerCard(BaseModel):
    """Real-time home energy snapshot."""

    household_w: float
    overflow_w: float
    cars_w: float
    pv_w: float


class GridCard(BaseModel):
    """Real-time grid exchange from available grid meters."""

    import_w: float | None
    export_w: float | None
    net_w: float | None
    tibber_net_w: float | None = None
    mt175_net_w: float | None = None


class ScheduleSlot(BaseModel):
    """One hourly slot in the MILP schedule."""

    # ISO-8601 UTC datetime of the slot start
    hour_iso: str
    # Planned power in W: negative = charge, positive = discharge, 0 = idle
    planned_w: float
    active: bool


class IntegrationCard(BaseModel):
    """Snapshot of one config-driven virtual integration."""

    name: str
    power_w: float | None = None


class StateResponse(BaseModel):
    """Full controller state snapshot returned by GET /api/state and SSE."""

    timestamp: str
    mode: str
    target_w: float
    maintenance_mode: bool
    batteries: list[BatteryCard]
    home_power: HomePowerCard | None
    grid: GridCard | None
    schedule: list[ScheduleSlot]
    integrations: list[IntegrationCard] = []

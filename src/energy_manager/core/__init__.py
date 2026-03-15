"""
Public surface of the core package.

Import the most commonly used symbols from here so callers can write:
    from energy_manager.core import Device, EventBus, DeviceState
"""

from .config import ConfigManager
from .constraint import Constraint
from .device import Device
from .event import DeviceStateEvent, Event, EventBus, PlanUpdatedEvent
from .forecast import ForecastProvider
from .models import (
    ConfigEntry,
    ControlAction,
    DeviceCategory,
    DeviceCommand,
    DeviceState,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    Measurement,
    TariffPoint,
)
from .optimizer import OptimizationContext, Optimizer
from .registry import DeviceRegistry
from .storage import StorageBackend
from .tariff import TariffModel

__all__ = [
    "ConfigEntry",
    "ConfigManager",
    "Constraint",
    "ControlAction",
    "Device",
    "DeviceCategory",
    "DeviceCommand",
    "DeviceState",
    "DeviceStateEvent",
    "EnergyPlan",
    "Event",
    "EventBus",
    "ForecastPoint",
    "ForecastProvider",
    "ForecastQuantity",
    "Measurement",
    "OptimizationContext",
    "Optimizer",
    "PlanUpdatedEvent",
    "DeviceRegistry",
    "StorageBackend",
    "TariffModel",
    "TariffPoint",
]

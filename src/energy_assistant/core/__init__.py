"""Core abstractions for Energy Assistant.

Re-exports the full public API of all core modules.
"""

from .config import AppConfig, BackendsConfig, ConfigManager, HomeAssistantConfig, IoBrokerConfig
from .constraint import Constraint
from .control import ControlContributor, ControlLoop, LiveSituation, StorageControlContributor
from .device import Device
from .event import DeviceStateEvent, Event, EventBus, PlanUpdatedEvent
from .forecast import ForecastProvider
from .models import (
    ConfigEntry,
    ControlIntent,
    DeviceCommand,
    DeviceRole,
    DeviceState,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    Measurement,
    StorageConstraints,
    TariffPoint,
)
from .optimizer import OptimizationContext, Optimizer
from .registry import DeviceRegistry
from .storage import StorageBackend
from .tariff import TariffModel
from .topology import TopologyNode, build_topology

__all__ = [
    "AppConfig",
    "BackendsConfig",
    "ConfigEntry",
    "ConfigManager",
    "Constraint",
    "ControlContributor",
    "ControlIntent",
    "ControlLoop",
    "Device",
    "DeviceCommand",
    "DeviceRegistry",
    "DeviceRole",
    "DeviceState",
    "DeviceStateEvent",
    "EnergyPlan",
    "Event",
    "EventBus",
    "ForecastPoint",
    "ForecastProvider",
    "ForecastQuantity",
    "HomeAssistantConfig",
    "IoBrokerConfig",
    "LiveSituation",
    "Measurement",
    "OptimizationContext",
    "Optimizer",
    "PlanUpdatedEvent",
    "StorageBackend",
    "StorageConstraints",
    "StorageControlContributor",
    "TariffModel",
    "TariffPoint",
    "TopologyNode",
    "build_topology",
]

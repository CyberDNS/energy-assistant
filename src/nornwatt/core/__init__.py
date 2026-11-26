"""Core abstractions for Nornwatt.

Re-exports the full public API of all core modules.
"""

from .config import (
    AppConfig,
    BackendsConfig,
    ConfigManager,
    HomeAssistantConfig,
)
from .constraint import Constraint
from .control import (
    ControlContributor,
    ControlLoop,
    LiveSituation,
    StorageControlContributor,
)
from .device import Device
from .device_model import (
    Capability,
    CategorySchema,
    ConnectionType,
    DeviceCategory,
    DeviceConfigError,
    DeviceSpec,
)
from .event import DeviceStateEvent, Event, EventBus, PlanUpdatedEvent
from .forecast import ForecastProvider
from .models import (
    ConfigEntry,
    ControlIntent,
    DeviceCommand,
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
    "Capability",
    "CategorySchema",
    "ConfigEntry",
    "ConfigManager",
    "ConnectionType",
    "Constraint",
    "ControlContributor",
    "ControlIntent",
    "ControlLoop",
    "Device",
    "DeviceCategory",
    "DeviceCommand",
    "DeviceConfigError",
    "DeviceRegistry",
    "DeviceSpec",
    "DeviceState",
    "DeviceStateEvent",
    "EnergyPlan",
    "Event",
    "EventBus",
    "ForecastPoint",
    "ForecastProvider",
    "ForecastQuantity",
    "HomeAssistantConfig",
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

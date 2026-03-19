"""Optimizer protocol and OptimizationContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from .models import DeviceState, EnergyPlan, ForecastPoint, ForecastQuantity, StorageConstraints

if TYPE_CHECKING:
    from .constraint import Constraint
    from .tariff import TariffModel


@dataclass
class OptimizationContext:
    """Everything the Optimizer needs to produce an EnergyPlan.

    Built by the planning loop and passed to the optimizer unchanged.

    Fields
    ------
    device_states:
        Latest state snapshot per device, keyed by ``device_id``.
    storage_constraints:
        Physical limits declared by storage devices.
    tariffs:
        Active tariff models keyed by ``tariff_id``.
    forecasts:
        Forecast series keyed by ``ForecastQuantity``.
    constraints:
        Active hard and soft constraints (e.g. EV charging deadlines).
    horizon:
        Planning window.  Defaults to 24 h.
    """

    device_states: dict[str, DeviceState]
    storage_constraints: list[StorageConstraints] = field(default_factory=list)
    tariffs: dict[str, "TariffModel"] = field(default_factory=dict)
    forecasts: dict[ForecastQuantity, list[ForecastPoint]] = field(default_factory=dict)
    constraints: list["Constraint"] = field(default_factory=list)
    horizon: timedelta = field(default_factory=lambda: timedelta(hours=24))


class Optimizer(Protocol):
    """Receives current state and forecasts; returns an EnergyPlan.

    The algorithm is a replaceable module.  The default is MILP (Mixed
    Integer Linear Programming via ``pulp``).  The same interface supports
    rule-based schedulers, ML models, or LLM-driven planners.
    """

    async def optimize(self, context: OptimizationContext) -> EnergyPlan:
        """Compute and return the optimal EnergyPlan for the given context."""
        ...

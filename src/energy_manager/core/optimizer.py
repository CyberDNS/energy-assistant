"""
Optimizer protocol and its input context.
"""

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
    """
    All information the optimizer needs to produce an EnergyPlan.

    ``device_states``
        Mapping of device_id → most recent DeviceState for every registered device.

    ``tariffs``
        Mapping of tariff_id → TariffModel.  Multiple tariffs can coexist
        (e.g. ``"hauptstrom"`` and ``"waermepumpe"``).  The optimizer looks up
        each device's applicable tariff via ``ConfigEntry.tariff_id``.
        A tariff registered under the id ``"default"`` is used as the fallback
        for devices that declare no explicit ``tariff_id``.

    ``forecasts``
        Mapping of ForecastQuantity → list of ForecastPoints for the planning horizon.
        May be empty if no forecast providers are configured.

    ``constraints``
        Active hard and soft constraints declared by device plugins or the user.

    ``horizon``
        How far ahead the optimizer should plan.
    """

    device_states: dict[str, DeviceState]
    # Devices that declare themselves controllable populate this list so the
    # optimizer can schedule them without knowing their concrete plugin type.
    storage_constraints: list[StorageConstraints] = field(default_factory=list)
    tariffs: dict[str, "TariffModel"] = field(default_factory=dict)
    forecasts: dict[ForecastQuantity, list[ForecastPoint]] = field(default_factory=dict)
    constraints: list["Constraint"] = field(default_factory=list)
    horizon: timedelta = field(default_factory=lambda: timedelta(hours=24))


class Optimizer(Protocol):
    async def optimize(self, context: OptimizationContext) -> EnergyPlan:
        """
        Analyse *context* and return a time-indexed schedule of control actions.
        """
        ...

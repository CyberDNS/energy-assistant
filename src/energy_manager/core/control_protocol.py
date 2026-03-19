"""
Control protocols and context for Energy Assistant.

Defines the structural interfaces that controllable devices implement, plus
the ``ControlContext`` snapshot that is passed to strategies on every control
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Switchable(Protocol):
    """Device that can be switched on or off."""

    async def turn_on(self) -> None:
        """Turn the device on."""
        ...

    async def turn_off(self) -> None:
        """Turn the device off."""
        ...


@runtime_checkable
class PowerControllable(Protocol):
    """Device that accepts a continuous power setpoint in watts."""

    min_power_w: float
    max_power_w: float

    async def set_power_w(self, watts: float) -> None:
        """Set the active power target in watts."""
        ...


@dataclass
class ControlContext:
    """
    Snapshot of real-time system state passed to strategies on each tick.

    Attributes
    ----------
    surplus_w:
        Available PV surplus (W).  Positive = currently exporting to grid.
        ``None`` when unknown.
    grid_power_w:
        Net grid power (W).  Positive = importing, negative = exporting.
        ``None`` when unknown.
    pv_power_w:
        Solar generation (W).  ``None`` when unknown.
    home_power_w:
        Total household consumption (W).  ``None`` when unknown.
    """

    surplus_w: float | None = None
    grid_power_w: float | None = None
    pv_power_w: float | None = None
    home_power_w: float | None = None


class ControlStrategyProtocol(Protocol):
    """Interface all control strategies must implement."""

    async def execute(self, context: ControlContext) -> None:
        """Execute the strategy for one control tick."""
        ...

"""Constraint — a hard or soft rule the optimizer must respect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import EnergyPlan


@dataclass
class Constraint:
    """A rule that an EnergyPlan must satisfy.

    Constraints are declared by device plugins or configured by the user.
    They are discovered dynamically at runtime — when an EV is not connected,
    no constraint is active; when it connects, its ``target_soc``/``target_by``
    constraint enters the next optimization run automatically.

    Subclasses implement ``is_satisfied`` for their specific logic.
    """

    device_id: str
    description: str
    is_hard: bool

    def is_satisfied(self, plan: "EnergyPlan") -> bool:
        """Return True if *plan* satisfies this constraint."""
        raise NotImplementedError

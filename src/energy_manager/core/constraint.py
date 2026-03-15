"""
Constraint base class.

Constraints are hard or soft rules the optimizer must respect.  They are
declared by device plugins or configured by the user.

Examples
--------
    - Battery SoC must not drop below 20 % (hard constraint from battery plugin)
    - EV must reach 80 % SoC by 07:00 (user-configured hard constraint)
    - Prefer not to export to grid when spot price is negative (soft constraint)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import EnergyPlan


@dataclass
class Constraint:
    """
    A rule the optimizer must (hard) or should (soft) respect.

    Subclass this and override ``is_satisfied`` to implement custom constraints.
    """

    device_id: str
    description: str
    is_hard: bool

    def is_satisfied(self, plan: "EnergyPlan") -> bool:
        """Return ``True`` if *plan* satisfies this constraint."""
        raise NotImplementedError

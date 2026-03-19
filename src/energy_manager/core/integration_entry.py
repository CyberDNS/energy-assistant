"""
Integration entry.

An ``IntegrationEntry`` is the complete descriptor for one config-driven
integration: the data source, its role in the energy system, and an optional
control strategy.
"""

from __future__ import annotations

from dataclasses import dataclass

from .integration import IntegrationSourceProtocol
from .control_protocol import ControlStrategyProtocol


@dataclass
class IntegrationEntry:
    """
    Full descriptor for one named integration.

    Attributes
    ----------
    name:
        Unique key used throughout the registry.
    role:
        Device role (``"source"``, ``"grid"``, ``"consumer"``, ``"storage"``,
        ``"ev_charger"``, ``"meter"``, or ``None`` if unspecified).
    source:
        Data-acquisition object (reads power / SoC / etc.).
    is_template:
        ``True`` when *source* is a Jinja2 template evaluated after all
        data-backed sources.  Set by the loader.
    strategy:
        Optional control strategy executed every control tick.  ``None``
        for read-only integrations.
    """

    name: str
    role: str | None
    source: IntegrationSourceProtocol
    is_template: bool = False
    strategy: ControlStrategyProtocol | None = None

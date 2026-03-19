"""
Integration registry.

Holds all named integration sources and their most-recently-read states.
``refresh_all()`` is called every control tick to update all states in parallel.
Template sources are evaluated *after* all data-backed sources so their
``states()`` calls see fresh readings.
"""

from __future__ import annotations

import asyncio
import logging

from .integration import IntegrationState, IntegrationSourceProtocol
from .integration_entry import IntegrationEntry
from .control_protocol import ControlContext

log = logging.getLogger(__name__)

# Fields exposed to templates via states("name.field").
_FIELD_MAP: dict[str, str] = {
    "power": "power_w",
}


class IntegrationRegistry:
    """
    Manages named integration sources and their latest states.

    Usage::

        registry = IntegrationRegistry()
        registry.register(GenericIoBrokerSource(...))
        registry.register_template(TemplateSource(...))
        await registry.refresh_all()
        state = registry.get_state("tibber")
    """

    def __init__(self) -> None:
        # Sources read in parallel (data-backed: ioBroker, HA, …)
        self._sources: list[IntegrationSourceProtocol] = []
        # Sources evaluated after all data sources (templates referencing others)
        self._template_sources: list[IntegrationSourceProtocol] = []
        self._states: dict[str, IntegrationState] = {}
        # Full integration entries (source + optional strategy)
        self._entries: dict[str, IntegrationEntry] = {}

    def register(self, source: IntegrationSourceProtocol) -> None:
        """Register a data-backed source."""
        self._sources.append(source)

    def register_template(self, source: IntegrationSourceProtocol) -> None:
        """Register a template source (evaluated after data sources)."""
        self._template_sources.append(source)

    def register_entry(self, entry: IntegrationEntry) -> None:
        """
        Register a full integration entry (source + role + optional strategy).

        Routes the source to ``_sources`` or ``_template_sources`` based on
        ``entry.is_template``.
        """
        self._entries[entry.name] = entry
        if entry.is_template:
            self._template_sources.append(entry.source)
        else:
            self._sources.append(entry.source)

    async def execute_strategies(self, context: ControlContext) -> None:
        """Execute all registered strategies with the current control context."""
        for entry in self._entries.values():
            if entry.strategy is not None:
                try:
                    await entry.strategy.execute(context)
                except Exception as exc:
                    log.warning(
                        "Strategy for integration %r failed: %s", entry.name, exc
                    )

    async def refresh_all(self) -> None:
        """
        Read all data sources concurrently, then evaluate template sources
        sequentially.  Failures are logged and swallowed so one bad source
        does not block the others.
        """
        if self._sources:
            results = await asyncio.gather(
                *(s.read() for s in self._sources),
                return_exceptions=True,
            )
            for i, result in enumerate(results):
                if isinstance(result, IntegrationState):
                    self._states[result.name] = result
                else:
                    log.warning(
                        "Integration source %r failed: %s",
                        self._sources[i].name,
                        result,
                    )

        for source in self._template_sources:
            try:
                state = await source.read()
                self._states[state.name] = state
            except Exception as exc:
                log.warning("Template source %r failed: %s", source.name, exc)

    def states(self, name_dot_field: str) -> str:
        """
        Look up a named integration's field value as a string.

        Called from Jinja2 templates as ``states("meter_name.field")`` where
        *field* is one of ``power``, ``power_import``, ``power_export``.
        Returns ``"unavailable"`` when the source has not been read yet or
        the reading failed.

        Example::

            states("grid_meter.power") | float | round(1)
        """
        if "." in name_dot_field:
            name, field = name_dot_field.split(".", 1)
        else:
            name, field = name_dot_field, "power"

        integration_state = self._states.get(name)
        if integration_state is None:
            return "unavailable"

        attr = _FIELD_MAP.get(field, field)
        value = getattr(integration_state, attr, None)
        return str(value) if value is not None else "unavailable"

    def get_state(self, name: str) -> IntegrationState | None:
        return self._states.get(name)

    def all_states(self) -> dict[str, IntegrationState]:
        return dict(self._states)

"""
Template integration source.

Evaluates a Jinja2 template to derive a power value from other named
integrations in the registry.  Configured via the ``template`` source type::

    integrations:
      - heatpump_meter:
          consumer:
            type: template
            power: |
              {% if states("grid_meter.power") | float - states("household_meter.power") | float >= 10 %}
              {{ states("grid_meter.power") | float - states("household_meter.power") | float }}
              {% else %}
              10
              {% endif %}

The ``states("name.field")`` function maps to ``IntegrationRegistry.states()``.
It returns the value as a string (like Home Assistant's template engine), so
use the ``| float`` filter to convert it for arithmetic.
Unavailable values return the string ``"unavailable"``; ``| float`` converts
that to ``0.0`` — guard with an ``{% if %}`` block if that matters.

Template sources are always evaluated *after* all data-backed sources so
that ``states()`` calls see the latest readings.
"""

from __future__ import annotations

from jinja2 import Environment, Undefined

from ...core.integration import IntegrationState


class TemplateSource:
    """
    Derives power from a Jinja2 template referencing other integrations.

    Parameters
    ----------
    name:
        Integration name.
    registry:
        The ``IntegrationRegistry`` whose ``states()`` method is exposed
        inside the template.
    power_template:
        Jinja2 template string that must evaluate to a number (W).
    """

    def __init__(
        self,
        name: str,
        registry: object,  # IntegrationRegistry — avoid circular import
        *,
        power_template: str,
    ) -> None:
        self.name = name
        self._registry = registry
        self._power_template = power_template.strip()
        self._env = Environment(undefined=Undefined)

    async def read(self) -> IntegrationState:
        template = self._env.from_string(self._power_template)
        result = template.render(states=self._registry.states).strip()
        try:
            power_w: float | None = (
                float(result)
                if result and result.lower() != "unavailable"
                else None
            )
        except (TypeError, ValueError):
            power_w = None
        return IntegrationState(name=self.name, power_w=power_w)

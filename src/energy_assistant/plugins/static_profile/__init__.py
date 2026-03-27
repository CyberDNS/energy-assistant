"""static_profile plugin — static time-of-day consumption forecast for consumer devices.

Registers the ``static_profile`` forecast type with the plugin registry.
Devices can declare this in their ``forecast:`` config section::

    - id: heatpump_meter
      role: consumer
      type: differential
      ...
      forecast:
        type: static_profile
        profile:
          weekdays:
            - hour: 0
              consumed_kwh: 0.5
          weekends:
            - hour: 0
              consumed_kwh: 10
"""

from __future__ import annotations

from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry


def register(registry: PluginRegistry) -> None:
    """Register the static_profile forecast factory."""
    registry.register_forecast("static_profile", _build)


def _build(forecast_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object:
    from .forecast import StaticProfileForecast

    return StaticProfileForecast(profile=cfg.get("profile", {}))

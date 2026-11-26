"""static_profile plugin — static time-of-day consumption forecast for consumer devices.

Registers the ``static_profile`` forecast type with the plugin registry.
Devices can declare this in their ``forecast:`` config section::

    - id: heatpump_meter
      category: consumer
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

from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry


def register(registry: PluginRegistry) -> None:
    """Register the static_profile forecast factory."""
    registry.register_forecast(
        "static_profile", _build,
        title="Static profile",
        description="Fixed consumption per time of day and day type.",
        scopes=("device",),
        settings={
            "profile": ParamSpec(object, required=True, widget="load_profile", label="Load profile",
                                 description="kWh consumed from each hour until the next entry, "
                                             "per day type"),
        },
    )


def _build(forecast_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object:
    from .forecast import StaticProfileForecast

    return StaticProfileForecast(profile=cfg.get("profile", {}))

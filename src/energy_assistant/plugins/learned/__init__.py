"""learned plugin — consumption forecast learned from Home Assistant statistics.

Registers the ``learned`` forecast type.  Devices declare it in their
``forecast:`` section::

    - id: heatpump_meter
      role: consumer
      type: differential
      ...
      forecast:
        type: learned
        history_entity: sensor.power_consumption_heatpump   # power sensor, W
        temperature_entity: sensor.outdoor_temperature      # optional
        weather_entity: weather.home                        # optional
        history_days: 730          # default
        refit_interval_h: 6        # default
        exclude:                   # optional — local time, [from, to)
          - { from: "2026-02-09 09:00", to: "2026-02-27 18:00" }
        exclude_when:              # optional — evaluated per hour
          - { entity: sensor.household_meter, condition: frozen }
          - { entity: sensor.power_consumption_household, below: -200 }
          - { entity: sensor.outdoor_temperature, condition: frozen, min_hours: 6 }
        fallback:                  # optional — used until a model is fitted
          type: static_profile
          profile: ...

Without ``temperature_entity`` the model learns a plain daily level and
hourly shape per day type, which suits habitual loads such as a household
baseline.  See ``core/learned_model.py`` for the model itself.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    """Register the learned forecast factory."""
    registry.register_forecast("learned", _build)


def _build(forecast_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object:
    from .. import registry as plugin_registry
    from .forecast import LearnedConsumptionForecast, parse_exclude, parse_exclude_when

    fallback = None
    fallback_cfg = cfg.get("fallback")
    if isinstance(fallback_cfg, dict):
        fallback = plugin_registry.build_forecast(f"{forecast_id}_fallback", fallback_cfg, ctx)

    if ctx.ha_client is None:
        _log.warning(
            "Learned forecast %r needs the homeassistant backend — using %s",
            forecast_id, "its fallback" if fallback is not None else "no forecast",
        )
        return fallback

    model_path = None
    if ctx.data_dir is not None:
        model_path = ctx.data_dir / "learned_models" / f"{forecast_id}.json"

    return LearnedConsumptionForecast(
        forecast_id,
        ctx.ha_client,
        history_entity=cfg["history_entity"],
        history_unit=cfg.get("history_unit", "W"),
        temperature_entity=cfg.get("temperature_entity"),
        weather_entity=cfg.get("weather_entity"),
        history_days=int(cfg.get("history_days", 730)),
        refit_interval_h=float(cfg.get("refit_interval_h", 6)),
        min_days=int(cfg.get("min_days", 14)),
        exclude=parse_exclude(cfg.get("exclude")),
        exclude_when=parse_exclude_when(cfg.get("exclude_when")),
        fallback=fallback,
        model_path=model_path,
    )

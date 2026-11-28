"""learned plugin — consumption forecast learned from Home Assistant statistics.

Registers the ``learned`` forecast type.  Devices declare it in their
``forecast:`` section::

    - id: heatpump_meter
      category: consumer
      type: differential
      ...
      forecast:
        type: learned
        history_entity: sensor.power_consumption_heatpump   # power sensor or energy meter
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

Residual baseline
-----------------
``subtract:`` lists sensors whose hourly values are taken off
``history_entity`` before fitting, so the model learns what is left::

    - id: baseline
      category: consumer
      type: generic_consumer
      forecast:
        type: learned
        history_entity: sensor.power_consumption_household
        subtract: [auto]            # every consumer with its own forecast
        fallback: { type: static_profile, profile: ... }

``auto`` is resolved by the device loader (``loader/forecast_config.py``):
it stands for the history entity of every other consumer on the same tariff
whose forecast is learned.  Adding or removing such a consumer changes the
residual on the next refit.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


#: Options of a learned forecast besides its ``history_entity``; also the
#: options of a consumer's ``learning:``.
LEARNING_OPTIONS = {
    "history_unit": ParamSpec(str, choices=("W", "kW"), advanced=True,
                              label="Unit of the sensor",
                              description="Only for a sensor that has no unit in Home "
                                          "Assistant: the unit its values are in"),
    "subtract": ParamSpec(list, items=ParamSpec(str, entity=True), label="Subtract sensors", item_label="sensor",
                          description="Sensors whose consumption is taken off before "
                                      "learning, so the forecast covers what is left. Enter "
                                      "auto for every other consumer on the same tariff that "
                                      "has a learned forecast"),
    "temperature_entity": ParamSpec(str, entity=True, label="Outdoor temperature sensor",
                                    description="For a load that follows the weather, such as "
                                                "a heat pump: the sensor of the outdoor "
                                                "temperature"),
    "weather_entity": ParamSpec(str, entity=True, label="Weather forecast entity",
                                description="Weather entity whose forecast gives the outdoor "
                                            "temperature of the coming hours"),
    "history_days": ParamSpec(int, minimum=1, default=730, advanced=True,
                              description="Days of history the forecast is learned from"),
    "min_days": ParamSpec(int, minimum=1, default=14, advanced=True,
                          label="Days needed",
                          description="Days with usable data needed before the learned "
                                      "forecast is used"),
    "refit_interval_h": ParamSpec(float, minimum=0, exclusive_minimum=True, default=6,
                                  advanced=True,
                                  description="Hours between two refits, when the forecast is "
                                              "learned again from the history"),
    "exclude": ParamSpec(list, label="Excluded periods", item_label="period", advanced=True,
                         description="Periods that are not learned from, e.g. a holiday or a "
                                     "time the sensor was wrong. In local time",
                         items=ParamSpec(dict, fields={
                             "from": ParamSpec(str, required=True, label="From (YYYY-MM-DD HH:MM)",
                                               description="Start of the period"),
                             "to": ParamSpec(str, required=True, label="To (YYYY-MM-DD HH:MM)",
                                             description="End of the period"),
                         })),
    "exclude_when": ParamSpec(list, label="Exclude while", item_label="condition",
                              description="Hours that are not learned from because of the "
                                          "state of an entity, e.g. while a meter was stuck",
                              items=ParamSpec(dict, fields={
                                  "entity": ParamSpec(str, required=True, entity=True,
                                                      description="The entity that is "
                                                                  "checked, hour by hour"),
                                  "condition": ParamSpec(
                                      str, choices=("frozen",),
                                      choice_labels={"frozen": "The value does not change"},
                                      description="Leaves out hours in which the value of the "
                                                  "entity did not change, e.g. a stuck meter"),
                                  "below": ParamSpec(float,
                                                     description="Leaves out hours in which "
                                                                 "the entity's mean value is "
                                                                 "below this"),
                                  "above": ParamSpec(float,
                                                     description="Leaves out hours in which "
                                                                 "the entity's mean value is "
                                                                 "above this"),
                                  "min_hours": ParamSpec(int, minimum=1, default=1,
                                                         label="For at least (h)",
                                                         description="Hours are only left out "
                                                                     "when the condition holds "
                                                                     "for this many hours in a "
                                                                     "row"),
                              })),
    "fallback": ParamSpec(dict, plugin="forecast", label="Fallback forecast",
                          description="Forecast used until enough history is there to learn "
                                      "from"),
}


def register(registry: PluginRegistry) -> None:
    """Register the learned forecast factory."""
    registry.register_forecast(
        "learned", _build,
        title="Learned from history",
        description="Consumption forecast learned from the history Home Assistant keeps of "
                    "a sensor.",
        scopes=("device",),
        settings={
            "history_entity": ParamSpec(str, required=True, entity=True, label="Sensor to learn from",
                                        description="Power or energy sensor of the consumer. "
                                                    "Its history in Home Assistant is what the "
                                                    "forecast is learned from"),
            **LEARNING_OPTIONS,
        },
    )


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
        history_unit=cfg.get("history_unit"),
        subtract=[str(e) for e in cfg.get("subtract") or [] if e != "auto"],
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

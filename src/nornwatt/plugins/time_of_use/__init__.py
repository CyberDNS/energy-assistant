"""time_of_use plugin — fixed prices by time window, weekday and season.

A classic day/night (HT/NT) tariff with cheap weekends::

    tariffs:
      household:
        type: time_of_use
        timezone: Europe/Berlin
        periods:
          - name: night
            price_eur_per_kwh: 0.24
            windows:
              - { days: [mon-fri], from: "22:00", to: "06:00" }
              - { days: [sat, sun], all_day: true }
          - name: day
            price_eur_per_kwh: 0.32
            default: true                 # everything no window covers
        seasons:                          # optional price overrides by date
          - { from: "11-01", to: "02-28", periods: { day: 0.34 } }
        export_price_eur_per_kwh: 0.08
        default: true

As a **component** on top of another tariff (``base``), its prices are added
to the base price, e.g. time-variable grid fees (§14a EnWG, Modul 3) on a
spot price.  Devices then use the composed tariff::

    tariffs:
      spot:
        type: tibber
        token: !secret tibber_token
      household:
        type: time_of_use
        base: spot
        periods:
          - { name: low,  price_eur_per_kwh: -0.06, windows: [{ from: "00:00", to: "06:00" }] }
          - { name: high, price_eur_per_kwh: 0.04,  windows: [{ from: "17:00", to: "21:00" }] }
          - { name: standard, price_eur_per_kwh: 0.0, default: true }
        default: true

See ``schedule`` for how windows, days and seasons are read.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry
from .schedule import parse_schedule

_log = logging.getLogger(__name__)

_WINDOW = ParamSpec(dict, fields={
    "days": ParamSpec(list, items=ParamSpec(str), label="Days", widget="weekdays",
                      description="Days of the week the window applies on. Left empty: every "
                                  "day"),
    "all_day": ParamSpec(bool, label="All day", widget="checkbox",
                         description="The window covers the whole day"),
    "from": ParamSpec(str, label="From", widget="time", when={"all_day": None},
                      description="Start of the window, on a quarter hour"),
    "to": ParamSpec(str, label="To", widget="time_end", when={"all_day": None},
                    description="End of the window. 24:00 is the end of the day; a time before "
                                "From runs over midnight"),
})

_PERIOD = ParamSpec(dict, summary="{name} · {price_eur_per_kwh} €/kWh", fields={
    "name": ParamSpec(str, required=True, label="Name",
                      description="Your name for the period, e.g. day or night"),
    "price_eur_per_kwh": ParamSpec(float, required=True, label="Price (€/kWh)",
                                   description="What a kWh costs in this period"),
    "default": ParamSpec(bool, label="All other times", widget="exclusive",
                         description="The period whose price applies whenever no time window "
                                     "covers the time. Without one, the windows must cover "
                                     "the whole week"),
    "windows": ParamSpec(list, items=_WINDOW, label="Time windows", item_label="time window",
                         description="The days and times this price applies"),
})

_SEASON = ParamSpec(dict, summary="{from} – {to}", fields={
    "from": ParamSpec(str, required=True, label="From", widget="month_day",
                      description="First day of the season"),
    "to": ParamSpec(str, required=True, label="To", widget="month_day",
                    description="Last day of the season. A day before From runs into the "
                                "next year"),
    "periods": ParamSpec(dict, required=True, values=ParamSpec(float),
                         keys=ParamSpec(str, label="Period", choices_from=("periods", "name")),
                         label="Prices (€/kWh)", item_label="price",
                         description="The price of each period during this season. Periods "
                                     "left out keep their usual price"),
})

#: What a new time-of-use tariff starts with: day/night with cheap weekends.
_EXAMPLE_PERIODS = [
    {"name": "night", "price_eur_per_kwh": 0.24, "windows": [
        {"days": ["mon-fri"], "from": "22:00", "to": "06:00"},
        {"days": ["sat", "sun"], "all_day": True},
    ]},
    {"name": "day", "price_eur_per_kwh": 0.32, "default": True},
]


def validate(cfg: Mapping[str, Any]) -> list[str]:
    """Problems of the windows and seasons (overlaps, gaps, bad times)."""
    return parse_schedule(cfg)[1]


def register(registry: PluginRegistry) -> None:
    registry.register_tariff(
        "time_of_use", _build_tariff,
        title="Time of use",
        description="Fixed prices by time window, weekday and season (e.g. day/night); "
                    "optionally added on top of another tariff.",
        settings={
            "periods": ParamSpec(list, required=True, items=_PERIOD, label="Periods",
                                 item_label="period", widget="tou_periods",
                                 initial=_EXAMPLE_PERIODS,
                                 description="The prices of the tariff, each with a name and "
                                             "the times it applies"),
            "seasons": ParamSpec(list, items=_SEASON, label="Seasons", advanced=True,
                                 item_label="season",
                                 description="Other prices for some periods during a part of "
                                             "the year, e.g. a winter price"),
            "timezone": ParamSpec(str, default="Europe/Berlin", label="Time zone",
                                  description="Time zone the times of the windows are in"),
            "export_price_eur_per_kwh": ParamSpec(float, default=0.0,
                                                  description="What you receive for every kWh "
                                                              "fed in"),
            "base": ParamSpec(str, tariff=True, widget="base_tariff", label="Added to tariff",
                              description="These prices are added to the prices of this "
                                          "tariff, e.g. time-dependent grid fees on a spot "
                                          "price. Left empty, the tariff stands alone"),
        },
        validator=validate,
    )


def _build_tariff(tariff_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .tariff import TimeOfUseTariff

    schedule, errors = parse_schedule(cfg)
    if schedule is None:
        _log.warning("Tariff %r (time_of_use): %s — skipping", tariff_id, "; ".join(errors))
        return None
    base = None
    base_id = cfg.get("base")
    if base_id:
        base = ctx.tariffs.get(base_id)
        if base is None:
            _log.warning("Tariff %r (time_of_use): base tariff %r is not available — skipping",
                         tariff_id, base_id)
            return None
    return TimeOfUseTariff(
        tariff_id=tariff_id,
        schedule=schedule,
        export_price_eur_per_kwh=float(cfg.get("export_price_eur_per_kwh", 0.0)),
        base=base,
    )

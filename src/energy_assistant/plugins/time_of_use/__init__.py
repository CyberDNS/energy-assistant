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
                      description="e.g. mon-fri, sat, sun (default: every day)"),
    "from": ParamSpec(str, label="From", description="HH:MM on a quarter hour"),
    "to": ParamSpec(str, label="To", description="HH:MM, 24:00 = end of day; "
                                                 "before 'from' = over midnight"),
    "all_day": ParamSpec(bool, label="All day", description="The whole day (no from/to)"),
})

_PERIOD = ParamSpec(dict, fields={
    "name": ParamSpec(str, required=True, label="Name", description="e.g. day, night"),
    "price_eur_per_kwh": ParamSpec(float, required=True, label="Price (€/kWh)"),
    "default": ParamSpec(bool, label="Default",
                         description="Applies whenever no window covers the time"),
    "windows": ParamSpec(list, items=_WINDOW, label="Windows"),
})

_SEASON = ParamSpec(dict, fields={
    "from": ParamSpec(str, required=True, label="From", description="MM-DD, inclusive"),
    "to": ParamSpec(str, required=True, label="To", description="MM-DD, inclusive"),
    "periods": ParamSpec(dict, required=True, values=ParamSpec(float), label="Prices (€/kWh)",
                         description="Price per period name in this season"),
})


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
                                 description="Named prices and the times they apply"),
            "seasons": ParamSpec(list, items=_SEASON, label="Seasons", advanced=True,
                                 description="Other prices for some periods within a date range"),
            "timezone": ParamSpec(str, default="Europe/Berlin", label="Timezone",
                                  description="Timezone the window times are in"),
            "export_price_eur_per_kwh": ParamSpec(float, default=0.0,
                                                  description="Payment for energy fed in (€/kWh)"),
            "base": ParamSpec(str, tariff=True, widget="base_tariff", label="Added to tariff",
                              description="Add these prices on top of this tariff "
                                          "(e.g. grid fees on a spot price); empty = standalone"),
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

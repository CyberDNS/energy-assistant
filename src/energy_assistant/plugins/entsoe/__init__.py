"""entsoe plugin — day-ahead spot price from the ENTSO-E Transparency Platform.

Covers dynamic electricity tariffs anywhere in the European day-ahead market:
the spot price of the bidding zone plus a formula for supplier markup, grid
fees, taxes and VAT.  Needs a free API token: register at
https://transparency.entsoe.eu, then ask for "Restful API access" (email to
transparency@entsoe.eu, subject "Restful API access"); the token is shown
under "My Account Settings" once granted.

Example config::

    tariffs:
      household:
        type: entsoe
        zone: DE-LU
        token: !secret entsoe_token
        formula:
          markup_eur_per_kwh: 0.025     # supplier margin
          grid_fee_eur_per_kwh: 0.09
          taxes_eur_per_kwh: 0.04       # levies, electricity tax
          vat: 0.19                     # applied to the sum, spot included
        export:
          type: spot                    # none (default) | spot | fixed
          factor: 1.0                   # spot: spot × factor + offset
        default: true

Import price = (spot + markup + grid fee + taxes) × (1 + vat); see
``tariff`` for the details.  Time-variable grid fees go on top with a
``time_of_use`` tariff whose ``base:`` is this tariff.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry
from .api import ZONES, zone_eic

_log = logging.getLogger(__name__)

_EXPORT_TYPES = ("none", "spot", "fixed")

_FORMULA = ParamSpec(dict, label="Price formula", fields={
    "markup_eur_per_kwh": ParamSpec(float, default=0.0, label="Supplier markup (€/kWh)",
                                    description="Margin of the supplier, before VAT"),
    "grid_fee_eur_per_kwh": ParamSpec(float, default=0.0, label="Grid fee (€/kWh)",
                                      description="Per-kWh grid charge, before VAT"),
    "taxes_eur_per_kwh": ParamSpec(float, default=0.0, label="Taxes and levies (€/kWh)",
                                   description="Electricity tax, levies, before VAT"),
    "vat": ParamSpec(float, default=0.0, minimum=0, maximum=1, label="VAT",
                     description="As a fraction (0.19 = 19 %), applied to the spot price plus "
                                 "all of the above"),
}, description="Price = (spot + markup + grid fee + taxes) × (1 + VAT)")

_EXPORT = ParamSpec(dict, label="Feed-in", fields={
    "type": ParamSpec(str, choices=_EXPORT_TYPES, default="none", label="Feed-in payment",
                      choice_labels={"none": "None", "spot": "Spot price",
                                     "fixed": "Fixed price"}),
    "factor": ParamSpec(float, default=1.0, label="Share of the spot price",
                        when={"type": "spot"},
                        description="Feed-in = spot × this + offset (1.0 = the full spot price)"),
    "offset_eur_per_kwh": ParamSpec(float, default=0.0, label="Offset (€/kWh)",
                                    when={"type": "spot"},
                                    description="Added to the feed-in price, e.g. -0.01 "
                                                "for a marketing fee"),
    "price_eur_per_kwh": ParamSpec(float, default=0.0, minimum=0, label="Price (€/kWh)",
                                   when={"type": "fixed"},
                                   description="Payment for energy fed in"),
}, description="What you receive for energy fed in")


def register(registry: PluginRegistry) -> None:
    registry.register_tariff(
        "entsoe", _build_tariff,
        title="Day-ahead spot price (ENTSO-E)",
        description="Spot price of your bidding zone from ENTSO-E, plus markup, grid fees, "
                    "taxes and VAT. Works for any dynamic tariff in Europe.",
        settings={
            "zone": ParamSpec(str, required=True, choices=tuple(ZONES), label="Bidding zone",
                              choice_labels={k: f"{name} ({k})" for k, (_, name) in ZONES.items()},
                              description="Market area of your connection"),
            "token": ParamSpec(str, required=True, secret=True, label="ENTSO-E API token",
                               description="Free token from transparency.entsoe.eu "
                                           "(ask for Restful API access)"),
            "formula": _FORMULA,
            "export": _EXPORT,
            "api_url": ParamSpec(str, advanced=True, label="ENTSO-E API URL",
                                 description="Tests only"),
        },
    )


def _float(d: dict[str, Any], key: str, default: float) -> float:
    v = d.get(key)
    return default if v in (None, "") else float(v)


def _build_tariff(tariff_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object | None:
    from .api import get_api
    from .tariff import EntsoeTariff, ExportRule, Formula

    token = str(cfg.get("token") or "").strip()
    if not token:
        _log.warning("Tariff %r (entsoe): 'token' is required — skipping", tariff_id)
        return None
    eic = zone_eic(str(cfg.get("zone") or ""))
    if eic is None:
        _log.warning("Tariff %r (entsoe): unknown bidding zone %r — skipping",
                     tariff_id, cfg.get("zone"))
        return None
    f = cfg.get("formula") or {}
    e = cfg.get("export") or {}
    export_type = str(e.get("type") or "none")
    if export_type not in _EXPORT_TYPES:
        _log.warning("Tariff %r (entsoe): unknown export type %r — no feed-in payment",
                     tariff_id, export_type)
        export_type = "none"
    return EntsoeTariff(
        tariff_id=tariff_id,
        api=get_api(token, eic, str(cfg.get("api_url") or "").strip() or None),
        formula=Formula(
            markup_eur_per_kwh=_float(f, "markup_eur_per_kwh", 0.0),
            grid_fee_eur_per_kwh=_float(f, "grid_fee_eur_per_kwh", 0.0),
            taxes_eur_per_kwh=_float(f, "taxes_eur_per_kwh", 0.0),
            vat=_float(f, "vat", 0.0),
        ),
        export=ExportRule(
            type=export_type,
            factor=_float(e, "factor", 1.0),
            offset_eur_per_kwh=_float(e, "offset_eur_per_kwh", 0.0),
            price_eur_per_kwh=_float(e, "price_eur_per_kwh", 0.0),
        ),
    )

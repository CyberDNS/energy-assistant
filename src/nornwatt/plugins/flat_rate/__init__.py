"""flat_rate plugin — constant import/export price tariff."""

from __future__ import annotations

from typing import Any

from ...core.device_model import ParamSpec
from ...core.plugin_registry import BuildContext, PluginRegistry


def register(registry: PluginRegistry) -> None:
    registry.register_tariff(
        "flat_rate", _build_tariff,
        title="Fixed price",
        description="Constant price per kWh.",
        settings={
            "import_price_eur_per_kwh": ParamSpec(float, minimum=0, default=0.0,
                                                  description="Price of energy drawn (€/kWh)"),
            "export_price_eur_per_kwh": ParamSpec(float, minimum=0, default=0.0,
                                                  description="Payment for energy fed in (€/kWh)"),
        },
    )


def _build_tariff(tariff_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object:
    from .tariff import FlatRateTariff
    return FlatRateTariff(
        tariff_id=tariff_id,
        import_price_eur_per_kwh=float(cfg.get("import_price_eur_per_kwh", 0.0)),
        export_price_eur_per_kwh=float(cfg.get("export_price_eur_per_kwh", 0.0)),
    )
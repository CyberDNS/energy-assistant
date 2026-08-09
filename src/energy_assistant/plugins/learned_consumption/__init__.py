"""learned_consumption plugin — self-updating consumption forecast.

Registers the ``learned_consumption`` forecast type with the plugin
registry. Unlike ``static_profile`` (a hand-authored curve), this predicts
consumption from a stratified regression model — bucketed by
``(hour_of_day, workday/weekend, anyone_home)`` and regressed against
outdoor temperature — that is periodically refit from measurement history by
the server's recompute loop (see ``core/learned_model.py`` and
``core/learned_model_store.py``).

Devices declare this in their ``forecast:`` config section::

    - id: heatpump_meter
      role: consumer
      type: differential
      ...
      forecast:
        type: learned_consumption
        history_days: 60          # optional, default 60
        min_samples_per_bucket: 10  # optional, default 10
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.plugin_registry import BuildContext, PluginRegistry

_log = logging.getLogger(__name__)


def register(registry: PluginRegistry) -> None:
    """Register the learned_consumption forecast factory."""
    registry.register_forecast("learned_consumption", _build)


def _build(forecast_id: str, cfg: dict[str, Any], ctx: BuildContext) -> object:
    from ...core.learned_model_store import LearnedModelConfig
    from .forecast import LearnedConsumptionForecast

    # Callers set cfg["_device_id"] (device_loader.build_device_forecasts,
    # server._build_tariff_weighted_price_forecast) since forecast_id's
    # naming convention isn't uniform across every call site — do NOT
    # derive device_id by parsing forecast_id.
    device_id = cfg.get("_device_id")
    if not device_id:
        device_id = forecast_id.removesuffix("_forecast")
        _log.warning(
            "learned_consumption: cfg['_device_id'] not set for forecast_id %r — "
            "falling back to stripping '_forecast', which may be wrong",
            forecast_id,
        )

    store = ctx.learned_model_store
    if store is not None:
        store.register(
            device_id,
            LearnedModelConfig(
                history_days=int(cfg.get("history_days", 60)),
                min_samples_per_bucket=int(cfg.get("min_samples_per_bucket", 10)),
            ),
        )

    return LearnedConsumptionForecast(
        device_id=device_id,
        model_store=store,
        ha_client=ctx.ha_client,
        environment=ctx.environment,
    )

"""In-memory holder for fitted LearnedConsumptionModel instances.

Bridges the periodic recompute loop (which fits models from history and
writes here) and the ``learned_consumption`` forecast plugin (which reads the
latest fitted model here to produce forecast points). A single instance is
created at server startup and threaded through ``BuildContext``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .learned_model import LearnedConsumptionModel


@dataclass
class LearnedModelConfig:
    """Per-device settings for fitting/recomputing a learned model."""

    history_days: int = 60
    min_samples_per_bucket: int = 10


class LearnedModelStore:
    """Thread-unsafe (single-event-loop) registry of device_id -> fitted model."""

    def __init__(self) -> None:
        self._models: dict[str, LearnedConsumptionModel] = {}
        self._configs: dict[str, LearnedModelConfig] = {}

    def register(self, device_id: str, config: LearnedModelConfig) -> None:
        """Declare that *device_id* should be fitted by the recompute loop,
        using *config*. Safe to call multiple times (e.g. on reload)."""
        self._configs[device_id] = config

    def configured_device_ids(self) -> list[str]:
        return list(self._configs.keys())

    def get_config(self, device_id: str) -> LearnedModelConfig | None:
        return self._configs.get(device_id)

    def get(self, device_id: str) -> LearnedConsumptionModel | None:
        return self._models.get(device_id)

    def set(self, device_id: str, model: LearnedConsumptionModel) -> None:
        self._models[device_id] = model

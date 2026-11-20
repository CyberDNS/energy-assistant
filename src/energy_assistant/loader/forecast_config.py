"""Effective forecast configuration per device.

Two rules turn the ``forecast:`` written in config.yaml into the one that is
built:

**Learning is on by default for basic consumers.**  A consumer that reads a
single Home Assistant power sensor (``generic_homeassistant``) gets a
``learned`` forecast on that sensor, unless it sets ``learning: false``.  A
``static_profile`` it declares becomes the fallback used until the first
model is fitted.  ``learning:`` may also be a dict of extra options for the
learned forecast (``temperature_entity``, ``history_days``, …).

**``subtract: [auto]`` is resolved.**  A learned forecast whose ``subtract``
list contains ``auto`` (a residual such as the household baseline) gets the
history entity of every other consumer on the same tariff whose effective
forecast is learned.  Consumers marked ``included_in: <device>`` are left
out: their load is already inside that device's meter, which is subtracted
as a whole.
"""

from __future__ import annotations

from typing import Any

from ..core.config import AppConfig
from ..core.device_model import Capability, DeviceCategory, DeviceSpec

AUTO = "auto"


def _ha_power_entity(spec: DeviceSpec) -> str | None:
    """The single HA power sensor a device reads, if it has exactly one."""
    if spec.type != "generic_homeassistant":
        return None
    return spec.bound_entity(Capability.POWER_W)


def default_learning_cfg(spec: DeviceSpec) -> dict[str, Any] | None:
    """Forecast config for *spec* with the default-learning rule applied.

    Returns the declared ``forecast:`` (or ``None``) when the rule does not
    apply.
    """
    declared = spec.forecast
    learning = spec.learning
    if learning is False or spec.category is not DeviceCategory.CONSUMER:
        return declared
    entity = _ha_power_entity(spec)
    if entity is None:
        return declared
    if declared is not None and declared.get("type") != "static_profile":
        return declared  # an explicit forecast of another kind wins
    out: dict[str, Any] = {"type": "learned", "history_entity": entity}
    if isinstance(learning, dict):
        out.update(learning)
    if declared is not None:
        out.setdefault("fallback", declared)
    return out


def effective_forecast_configs(app_config: AppConfig) -> dict[str, dict[str, Any]]:
    """Forecast config per device id, for every device that has one."""
    default_tariff = app_config.default_tariff_id
    forecasts: dict[str, dict[str, Any]] = {}
    for device_id, spec in app_config.devices.items():
        fc = default_learning_cfg(spec)
        if fc is not None:
            forecasts[device_id] = fc

    def tariff(device_id: str) -> Any:
        return app_config.devices[device_id].tariff or default_tariff

    def is_residual(fc: dict[str, Any]) -> bool:
        return fc.get("type") == "learned" and AUTO in (fc.get("subtract") or [])

    for device_id, fc in list(forecasts.items()):
        if not is_residual(fc):
            continue
        own = fc.get("history_entity")
        entities: list[str] = []
        for other_id, other_fc in forecasts.items():
            other = app_config.devices[other_id]
            if (
                other_id == device_id
                or other.category is not DeviceCategory.CONSUMER
                or other.included_in
                or other_fc.get("type") != "learned"
                or is_residual(other_fc)
                or tariff(other_id) != tariff(device_id)
            ):
                continue
            entity = other_fc.get("history_entity")
            if entity and entity != own and entity not in entities:
                entities.append(entity)
        explicit = [e for e in fc.get("subtract") or [] if e != AUTO and e not in entities]
        forecasts[device_id] = {**fc, "subtract": entities + explicit}
    return forecasts

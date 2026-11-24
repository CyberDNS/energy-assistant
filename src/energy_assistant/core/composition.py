"""Composition: from configured devices to optimizer and control inputs.

The optimizer (``OptimizationContext``) and the control contributors keep
their shape; this step derives their inputs from the ``DeviceSpec`` list
(``core/device_model``):

==========================  ==========================================
devices                     composed into
==========================  ==========================================
``battery``                 ``StorageConstraints`` (from ``params``)
``thermal_load`` + switch   ``ThresholdConstraints`` (thresholds and rates
                            from the load, rated power from its switch)
``metered_by`` connections  the metering tree (``TopologyNode``)
grid meter ``params``       the grid connection limit on the tree root
==========================  ==========================================

Vehicles and chargers are composed at runtime by ``assets/fleet.py``
(their sessions change while the app runs).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .device_model import DeviceCategory, DeviceSpec
from .models import StorageConstraints, ThresholdConstraints
from .topology import TopologyNode, build_topology


@dataclass
class Composition:
    """Everything composed from the device list."""

    topology: TopologyNode | None
    storage_constraints: list[StorageConstraints]
    threshold_constraints: list[ThresholdConstraints]


def compose(devices: Mapping[str, DeviceSpec]) -> Composition:
    """Compose the optimizer / control inputs from validated *devices*."""
    return Composition(
        topology=build_topology(devices),
        storage_constraints=storage_constraints(devices),
        threshold_constraints=threshold_constraints(devices),
    )


def storage_constraints(devices: Mapping[str, DeviceSpec]) -> list[StorageConstraints]:
    """``StorageConstraints`` for every ``battery``, in config order."""
    return [
        _storage_constraint(spec)
        for spec in devices.values()
        if spec.category is DeviceCategory.BATTERY
    ]


def _storage_constraint(spec: DeviceSpec) -> StorageConstraints:
    p = spec.params
    cycle_life = p.get("cycle_life")
    purchase_price = p.get("purchase_price_eur")
    return StorageConstraints(
        device_id=spec.id,
        capacity_kwh=float(p["capacity_kwh"]),
        max_charge_kw=float(p["max_charge_kw"]),
        max_discharge_kw=float(p["max_discharge_kw"]),
        charge_efficiency=float(p.get("charge_efficiency", 0.95)),
        discharge_efficiency=float(p.get("discharge_efficiency", 0.95)),
        min_soc_pct=float(p.get("min_soc_pct", 0.0)),
        max_soc_pct=float(p.get("max_soc_pct", 100.0)),
        purchase_price_eur=float(purchase_price) if purchase_price is not None else None,
        cycle_life=int(cycle_life) if cycle_life is not None else None,
        no_grid_charge=bool(p.get("no_grid_charge", False)),
        own_meter=str(p.get("own_meter", "zone")),
        standby_w=float(p.get("standby_w", 50.0)),
    )


def threshold_constraints(devices: Mapping[str, DeviceSpec]) -> list[ThresholdConstraints]:
    """``ThresholdConstraints`` for every ``thermal_load``, in config order.

    The rated power is the switch's (``controlled_by``): the switch is the
    means, the thermal load holds the needs (thresholds, rates, run times).
    A thermal load that switches itself (a thermostat) has its own
    ``rated_power_w``.
    """
    out = []
    for spec in devices.values():
        if spec.category is not DeviceCategory.THERMAL_LOAD:
            continue
        p = spec.params
        switch = devices[spec.controlled_by] if spec.controlled_by else None
        rated_w = (switch.params.get("rated_power_w") if switch is not None
                   else p.get("rated_power_w"))
        if rated_w is None:  # validate_connections reports this
            continue
        out.append(ThresholdConstraints(
            device_id=spec.id,
            bottom_threshold=float(p["bottom_threshold"]),
            top_threshold=float(p["top_threshold"]),
            unit=str(p.get("unit", "°C")),
            direction=str(p.get("direction", "reduces")),  # type: ignore[arg-type]
            rated_power_kw=float(rated_w) / 1000.0,
            active_rate_per_h=float(p["active_rate_per_h"]),
            drift_rate_per_h=float(p["drift_rate_per_h"]),
            min_runtime_h=float(p.get("min_runtime_h", 0.0)),
            min_offtime_h=float(p.get("min_offtime_h", 0.0)),
            label=spec.label or "",
        ))
    return out

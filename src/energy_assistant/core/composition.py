"""Composition: from configured devices to optimizer and control inputs.

The optimizer (``OptimizationContext``) and the control contributors keep
their shape; this step derives their inputs from the ``DeviceSpec`` list
(``core/device_model``):

==========================  ==========================================
devices                     composed into
==========================  ==========================================
``battery``                 ``StorageConstraints`` (from ``params``)
``metered_by`` connections  the metering tree (``TopologyNode``)
grid meter ``params``       the grid connection limit on the tree root
==========================  ==========================================

EVs and threshold loads still come from the ``assets:`` section until
vehicles, chargers, thermal loads and switches are composed here as well.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .device_model import DeviceCategory, DeviceSpec
from .models import StorageConstraints
from .topology import TopologyNode, build_topology


@dataclass
class Composition:
    """Everything composed from the device list."""

    topology: TopologyNode | None
    storage_constraints: list[StorageConstraints]


def compose(devices: Mapping[str, DeviceSpec]) -> Composition:
    """Compose the optimizer / control inputs from validated *devices*."""
    return Composition(
        topology=build_topology(devices),
        storage_constraints=storage_constraints(devices),
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
        standby_w=float(p.get("standby_w", 50.0)),
    )

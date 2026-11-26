"""TopologyNode — tree structure representing the physical wiring of meters.

The tree is built from the ``metered_by`` connection of each device
(``core/device_model``): the meter without ``metered_by`` is the root (the
grid connection), and every other device lists the meter it sits behind::

    devices:
      - id: main_grid_meter
        category: meter            # no metered_by: the grid connection
        params:
          max_import_kw: 24        # optional grid connection limit
          curtail_order: [home_battery, wallbox]   # optional: curtailed first → last
          limit_hysteresis_kw: 1.0 # optional, default 5 % of the limit
      - id: household_meter
        category: meter
        metered_by: main_grid_meter
      - id: home_battery
        category: battery
        metered_by: household_meter

Children keep the order of the ``devices:`` list.  Devices without
``metered_by`` (other than the grid meter) are not part of the tree.

The tree is used for:

- **Residual derivation** — infer an un-metered device's power as
  ``parent.power_w − sum(directly-metered children)``.
- **Cost attribution** — compute per-branch costs using the tariff
  declared on the device at each node.
- **Power-flow visualisation** — render where energy is flowing.

Grid connection limit
---------------------
The planner keeps planned import below ``max_import_kw``; the control loop
enforces it every tick by reducing flexible loads (see
``core/grid_limit.py``).  Without ``curtail_order``, storage charging is
curtailed before EV charging, each in registration order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .device_model import is_grid_meter

if TYPE_CHECKING:
    from .device_model import DeviceSpec


@dataclass
class TopologyNode:
    """A single node in the topology tree.

    Parameters
    ----------
    device_id:
        Matches a ``device_id`` in ``DeviceRegistry``.
    children:
        Sub-nodes whose energy flows through this node.
    """

    device_id: str
    children: list[TopologyNode] = field(default_factory=list)
    max_import_kw: float | None = None
    """Connection limit on import through this node (kW); root node only."""
    curtail_order: list[str] = field(default_factory=list)
    """Device ids curtailed first → last when the limit is reached."""
    limit_hysteresis_kw: float | None = None
    """Headroom below the limit required before curtailment is released."""

    def find(self, device_id: str) -> TopologyNode | None:
        """Return the node with *device_id*, searching depth-first, or None."""
        if self.device_id == device_id:
            return self
        for child in self.children:
            result = child.find(device_id)
            if result is not None:
                return result
        return None

    def all_device_ids(self) -> list[str]:
        """Return all device IDs in this subtree, depth-first."""
        ids = [self.device_id]
        for child in self.children:
            ids.extend(child.all_device_ids())
        return ids


def build_topology(devices: Mapping[str, DeviceSpec]) -> TopologyNode | None:
    """Build the metering tree from the devices' ``metered_by`` connections.

    *devices* must be validated (``device_model.parse_devices``): at most one
    grid connection meter, and every ``metered_by`` chain ends at it.  Returns
    ``None`` without a meter.
    """
    root_spec = next((s for s in devices.values() if is_grid_meter(s)), None)
    if root_spec is None:
        return None
    children: dict[str, list[str]] = {}
    for spec in devices.values():
        if spec.metered_by is not None:
            children.setdefault(spec.metered_by, []).append(spec.id)

    def node(device_id: str) -> TopologyNode:
        return TopologyNode(
            device_id=device_id,
            children=[node(c) for c in children.get(device_id, [])],
        )

    root = node(root_spec.id)
    params = root_spec.params
    if (limit := params.get("max_import_kw")) is not None:
        root.max_import_kw = float(limit)
    root.curtail_order = [str(d) for d in params.get("curtail_order") or []]
    if (hyst := params.get("limit_hysteresis_kw")) is not None:
        root.limit_hysteresis_kw = max(0.0, float(hyst))
    return root

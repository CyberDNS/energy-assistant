"""TopologyNode — tree structure representing the physical wiring of meters.

The topology describes *structural wiring* only.  Device capabilities and
sources are declared in the ``devices:`` config section and the
``DeviceRegistry`` — not here.

The topology tree is used for:

- **Residual derivation** — infer an un-metered device's power as
  ``parent.power_w − sum(directly-metered children)``.
- **Cost attribution** — compute per-branch costs using the tariff
  declared on the device at each node.
- **Power-flow visualisation** — render where energy is flowing.

YAML shape
----------
The ``topology:`` section is a single-entry dict whose key is the
root device ID.  Children are listed under a ``children:`` key::

    topology:
      main_grid_meter:
        children:
          - household_meter
          - heatpump

Nodes without children may be listed as plain strings in their parent's
``children`` list.  A node with its own children may be a nested dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    children: list["TopologyNode"] = field(default_factory=list)

    def find(self, device_id: str) -> "TopologyNode | None":
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


def build_topology(cfg: "list[dict[str, Any]] | None") -> "TopologyNode | None":
    """Parse the ``topology:`` config section and return the root node.

    The expected format is a single-item list containing one dict::

        topology:
          - main_grid_meter:
              children:
                - household_meter:
                    children:
                      - heatpump_meter

    Returns ``None`` when *cfg* is empty or ``None``.

    Raises
    ------
    ValueError
        When the list contains more than one root entry.
    """
    if not cfg:
        return None

    if not isinstance(cfg, list) or not all(isinstance(item, dict) for item in cfg):
        raise TypeError(
            "topology: expected a list of dicts, "
            f"got {type(cfg).__name__!r}"
        )

    if len(cfg) != 1:
        raise ValueError(
            f"topology: must have exactly one root entry, got {len(cfg)}"
        )

    root_entry = cfg[0]
    if len(root_entry) != 1:
        raise ValueError(
            f"topology: root entry must have exactly one key, "
            f"got {list(root_entry)!r}"
        )

    root_id, subtree = next(iter(root_entry.items()))
    return _parse_node(root_id, subtree or {})


def _parse_node(device_id: str, subtree: dict | list | None) -> TopologyNode:
    children: list[TopologyNode] = []

    if isinstance(subtree, dict):
        for child_entry in subtree.get("children", []):
            if isinstance(child_entry, str):
                children.append(TopologyNode(device_id=child_entry))
            elif isinstance(child_entry, dict):
                for child_id, child_sub in child_entry.items():
                    children.append(_parse_node(child_id, child_sub or {}))

    elif isinstance(subtree, list):
        for item in subtree:
            if isinstance(item, str):
                children.append(TopologyNode(device_id=item))

    return TopologyNode(device_id=device_id, children=children)

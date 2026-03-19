"""Tests for TopologyNode and build_topology."""

from __future__ import annotations

import pytest

from energy_assistant.core.topology import TopologyNode, build_topology


class TestTopologyNode:
    def test_find_self(self) -> None:
        node = TopologyNode("root")
        assert node.find("root") is node

    def test_find_direct_child(self) -> None:
        child = TopologyNode("child")
        root = TopologyNode("root", children=[child])
        assert root.find("child") is child

    def test_find_grandchild(self) -> None:
        grandchild = TopologyNode("gc")
        child = TopologyNode("c", children=[grandchild])
        root = TopologyNode("root", children=[child])
        assert root.find("gc") is grandchild

    def test_find_missing_returns_none(self) -> None:
        root = TopologyNode("root")
        assert root.find("missing") is None

    def test_all_device_ids(self) -> None:
        root = TopologyNode(
            "main_grid_meter",
            children=[
                TopologyNode("household_meter"),
                TopologyNode("heatpump"),
            ],
        )
        ids = root.all_device_ids()
        assert set(ids) == {"main_grid_meter", "household_meter", "heatpump"}
        assert ids[0] == "main_grid_meter"  # root first


class TestBuildTopology:
    def test_empty_config_returns_none(self) -> None:
        assert build_topology({}) is None

    def test_single_root_no_children(self) -> None:
        root = build_topology({"main_grid_meter": {}})
        assert root is not None
        assert root.device_id == "main_grid_meter"
        assert root.children == []

    def test_root_with_string_children(self) -> None:
        cfg = {
            "main_grid_meter": {
                "children": ["household_meter", "heatpump"]
            }
        }
        root = build_topology(cfg)
        assert root is not None
        assert root.device_id == "main_grid_meter"
        child_ids = [c.device_id for c in root.children]
        assert child_ids == ["household_meter", "heatpump"]

    def test_nested_children(self) -> None:
        cfg = {
            "main_grid_meter": {
                "children": [
                    {
                        "household_meter": {
                            "children": ["sub_circuit_a"]
                        }
                    },
                    "heatpump",
                ]
            }
        }
        root = build_topology(cfg)
        assert root is not None
        household = root.find("household_meter")
        assert household is not None
        assert household.children[0].device_id == "sub_circuit_a"

    def test_multiple_roots_raises(self) -> None:
        with pytest.raises(ValueError, match="exactly one root"):
            build_topology({"meter1": {}, "meter2": {}})

    def test_messkonzept8_topology(self) -> None:
        """Full Messkonzept 8 topology parses correctly."""
        cfg = {
            "main_grid_meter": {
                "children": ["household_meter", "heatpump"]
            }
        }
        root = build_topology(cfg)
        assert root is not None
        assert root.device_id == "main_grid_meter"
        assert root.find("household_meter") is not None
        assert root.find("heatpump") is not None
        all_ids = set(root.all_device_ids())
        assert all_ids == {"main_grid_meter", "household_meter", "heatpump"}

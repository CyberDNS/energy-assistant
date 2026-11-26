"""Tests for TopologyNode and build_topology."""

from __future__ import annotations

from nornwatt.core.device_model import parse_devices
from nornwatt.core.topology import TopologyNode, build_topology


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


def _devices(*entries: dict) -> dict:
    """Parse ``devices:`` entries (list form) into validated specs."""
    return parse_devices({e["id"]: {k: v for k, v in e.items() if k != "id"} for e in entries})


def _meter(device_id: str, category: str = "meter", **kw) -> dict:
    return {"id": device_id, "category": category, "type": "tasmota", "host": "h", **kw}


class TestBuildTopology:
    def test_no_meter_returns_none(self) -> None:
        assert build_topology({}) is None

    def test_the_meter_without_metered_by_is_the_root(self) -> None:
        root = build_topology(_devices(
            _meter("household_meter", metered_by="main_grid_meter"), _meter("main_grid_meter"),
        ))
        assert root is not None and root.device_id == "main_grid_meter"

    def test_grid_meter_without_children(self) -> None:
        root = build_topology(_devices(_meter("main_grid_meter")))
        assert root is not None
        assert root.device_id == "main_grid_meter"
        assert root.children == []

    def test_children_keep_config_order(self) -> None:
        root = build_topology(_devices(
            _meter("main_grid_meter"),
            _meter("household_meter", metered_by="main_grid_meter"),
            {"id": "heatpump", "category": "consumer", "type": "differential",
             "minuend": "main_grid_meter", "subtrahend": "household_meter",
             "metered_by": "main_grid_meter"},
        ))
        assert root is not None
        assert [c.device_id for c in root.children] == ["household_meter", "heatpump"]

    def test_nested_children(self) -> None:
        root = build_topology(_devices(
            _meter("main_grid_meter"),
            _meter("household_meter", metered_by="main_grid_meter"),
            _meter("sub_circuit_a", metered_by="household_meter"),
        ))
        assert root is not None
        household = root.find("household_meter")
        assert household is not None
        assert household.children[0].device_id == "sub_circuit_a"

    def test_devices_without_metered_by_are_not_in_the_tree(self) -> None:
        root = build_topology(_devices(
            _meter("main_grid_meter"),
            {"id": "baseline", "category": "consumer", "type": "generic_consumer"},
        ))
        assert root is not None
        assert root.all_device_ids() == ["main_grid_meter"]

    def test_grid_limit_from_grid_meter_params(self) -> None:
        root = build_topology(_devices(
            _meter("main_grid_meter", params={
                "max_import_kw": 24, "curtail_order": ["household_meter"],
                "limit_hysteresis_kw": 1.0,
            }),
            _meter("household_meter", metered_by="main_grid_meter"),
        ))
        assert root is not None
        assert root.max_import_kw == 24.0
        assert root.curtail_order == ["household_meter"]
        assert root.limit_hysteresis_kw == 1.0

    def test_messkonzept8_tree(self) -> None:
        """The live installation: Z1 → Z2 → PV + batteries."""
        root = build_topology(_devices(
            _meter("main_grid_meter"),
            _meter("household_meter", metered_by="main_grid_meter"),
            {"id": "pv_production", "category": "pv", "type": "generic_homeassistant",
             "metered_by": "household_meter",
             "capabilities": {"power_w": {"entity": "sensor.pv", "invert_sign": True}}},
            {"id": "sma_battery", "category": "battery", "type": "modbus",
             "metered_by": "household_meter", "profile": "p", "host": "h",
             "params": {"capacity_kwh": 7.4, "max_charge_kw": 3, "max_discharge_kw": 3}},
        ))
        assert root is not None
        assert root.device_id == "main_grid_meter"
        assert [c.device_id for c in root.children] == ["household_meter"]
        assert [c.device_id for c in root.children[0].children] == ["pv_production", "sma_battery"]

"""Tests for EV chargepoint control: disabled handling, reconciliation,
and the control-loop grid correction excluding EV consumption."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.assets.ev import (
    EvChargerContributor,
    EvChargingAsset,
    build_goal_from_parts,
)
from energy_assistant.core.control import ControlLoop, LiveSituation, StorageControlContributor
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import (
    ControlIntent,
    DeviceCommand,
    DeviceState,
    StorageConstraints,
)
from energy_assistant.plugins.openwb_homeassistant.device import OpenWBDevice
from tests.helpers.fake_ha_client import FakeHAClient


def _asset(device_id: str = "wallbox") -> EvChargingAsset:
    return EvChargingAsset(
        asset_id="ev1",
        device_id=device_id,
        label="EV",
        capacity_kwh=60.0,
        max_charge_kw=11.0,
    )


def _live(
    now: datetime,
    *,
    grid_power_w: float = 0.0,
    device_states: dict[str, DeviceState] | None = None,
) -> LiveSituation:
    return LiveSituation(
        timestamp=now,
        grid_power_w=grid_power_w,
        dt_hours=30 / 3600,
        device_states=device_states or {},
    )


# ---------------------------------------------------------------------------
# EvChargerContributor — disabled behaviour
# ---------------------------------------------------------------------------


def test_disabled_chargepoint_sends_no_command() -> None:
    """A disabled chargepoint is fully hands-off: no command at all (not even
    Stop), so the wallbox controls charging itself — even with an active goal
    and a charge in progress."""
    now = datetime.now(timezone.utc)
    contrib = EvChargerContributor(_asset())
    goal = build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=1.38, charge_limit_soc_pct=80.0,
        target_soc_pct=80.0, target_by=now + timedelta(hours=6),
        charge_curve=[], current_soc_pct=40.0, connected=True,
    )
    contrib.update_goal(goal)
    contrib.set_disabled(True)

    state = DeviceState(device_id="wallbox", power_w=11_000.0, soc_pct=40.0, available=True)
    live = _live(now, device_states={"wallbox": state})

    assert contrib.desired_setpoint_w(None, live) is None

    # Re-enabled with a goal below the charge limit → control resumes.
    contrib.set_disabled(False)
    assert contrib.desired_setpoint_w(None, live) is not None


# ---------------------------------------------------------------------------
# EvChargerContributor — intent translation must respect grid_allowed
# ---------------------------------------------------------------------------


def _scheduled_goal(now: datetime, current_soc: float = 40.0):
    return build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=now + timedelta(hours=12),
        charge_curve=[], current_soc_pct=current_soc, connected=True,
    )


def _ev_intent(now: datetime, power_kw: float, grid_allowed: bool) -> ControlIntent:
    return ControlIntent(
        device_id="wallbox", timestep=now, power_kw=power_kw,
        grid_allowed=grid_allowed,
    )


def test_pv_sourced_intent_maps_to_pv_mode_not_instant() -> None:
    """A planned charging slot with grid_allowed=False is PV-surplus energy —
    it must map to the PV sentinel, not instant charging (which would pull
    the shortfall from the grid, contradicting the plan and the UI)."""
    now = datetime.now(timezone.utc)
    contrib = EvChargerContributor(_asset())
    contrib.update_goal(_scheduled_goal(now))
    state = DeviceState(device_id="wallbox", power_w=0.0, soc_pct=40.0, available=True)
    live = _live(now, device_states={"wallbox": state})

    setpoint = contrib.desired_setpoint_w(_ev_intent(now, 4.8, grid_allowed=False), live)
    assert setpoint is not None
    assert 0.0 < setpoint <= 500.0  # PV sentinel range


def test_grid_allowed_intent_maps_to_instant() -> None:
    now = datetime.now(timezone.utc)
    contrib = EvChargerContributor(_asset())
    contrib.update_goal(_scheduled_goal(now))
    state = DeviceState(device_id="wallbox", power_w=0.0, soc_pct=40.0, available=True)
    live = _live(now, device_states={"wallbox": state})

    setpoint = contrib.desired_setpoint_w(_ev_intent(now, 4.8, grid_allowed=True), live)
    assert setpoint == 4800.0  # instant charging at the planned power


# ---------------------------------------------------------------------------
# PV priority between multiple EVs
# ---------------------------------------------------------------------------


def _two_ev_loop(now: datetime, *, plan_intents: list[ControlIntent]):
    """Two EV contributors (wallbox_a, wallbox_b) in a ControlLoop with plan."""
    from energy_assistant.core.models import EnergyPlan

    loop = ControlLoop(ledger=BatteryCostLedger())
    contribs = {}
    for dev in ("wallbox_a", "wallbox_b"):
        asset = EvChargingAsset(
            asset_id=f"ev_{dev}", device_id=dev, label=dev,
            capacity_kwh=60.0, max_charge_kw=11.0,
        )
        c = EvChargerContributor(asset)
        c.update_goal(build_goal_from_parts(
            asset_id=f"ev_{dev}", device_id=dev, capacity_kwh=60.0,
            max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
            target_soc_pct=90.0, target_by=now + timedelta(hours=12),
            charge_curve=[], current_soc_pct=50.0, connected=True,
        ))
        loop.register_contributor(c)
        contribs[dev] = c
    loop.update_plan(EnergyPlan(intents=plan_intents))
    return loop, contribs


def _two_ev_live(now: datetime) -> LiveSituation:
    return _live(now, device_states={
        "wallbox_a": DeviceState(device_id="wallbox_a", power_w=0.0, soc_pct=50.0, available=True),
        "wallbox_b": DeviceState(device_id="wallbox_b", power_w=0.0, soc_pct=50.0, available=True),
    })


def test_unplanned_ev_yields_pv_surplus_to_planned_ev() -> None:
    """When the optimizer allocated this slot's PV to wallbox_a, wallbox_b
    must command Stop instead of competing in openWB PV mode."""
    now = datetime.now(timezone.utc)
    loop, _ = _two_ev_loop(now, plan_intents=[
        ControlIntent(device_id="wallbox_a", timestep=now, power_kw=5.0, grid_allowed=False),
    ])

    setpoints = {d: w for d, w, _m in loop.describe_setpoints(_two_ev_live(now))}
    assert 0.0 < setpoints["wallbox_a"] <= 500.0   # PV sentinel — the planned EV
    assert setpoints["wallbox_b"] == 0.0            # yields (Stop)


def test_both_planned_evs_stay_in_pv_mode() -> None:
    now = datetime.now(timezone.utc)
    loop, _ = _two_ev_loop(now, plan_intents=[
        ControlIntent(device_id="wallbox_a", timestep=now, power_kw=5.0, grid_allowed=False),
        ControlIntent(device_id="wallbox_b", timestep=now, power_kw=3.0, grid_allowed=False),
    ])

    setpoints = {d: w for d, w, _m in loop.describe_setpoints(_two_ev_live(now))}
    assert 0.0 < setpoints["wallbox_a"] <= 500.0
    assert 0.0 < setpoints["wallbox_b"] <= 500.0


def test_no_planned_ev_keeps_opportunistic_pv() -> None:
    """With no PV allocation anywhere, both EVs may absorb surplus."""
    now = datetime.now(timezone.utc)
    loop, _ = _two_ev_loop(now, plan_intents=[])

    setpoints = {d: w for d, w, _m in loop.describe_setpoints(_two_ev_live(now))}
    assert 0.0 < setpoints["wallbox_a"] <= 500.0
    assert 0.0 < setpoints["wallbox_b"] <= 500.0


def test_stale_sparse_ev_intent_expires_and_yields() -> None:
    """EV intents are sparse (only charging slots get one).  A PV slot that
    ended must not stay 'active' via the most-recent-≤-now lookup — the live
    bug: Banzert's finished 11:15 PV slot kept claiming the allocation at
    11:37 while Schlumpf held the current slot, so nobody yielded."""
    now = datetime.now(timezone.utc)
    loop, _ = _two_ev_loop(now, plan_intents=[
        # wallbox_a: PV slot that ended 30 min ago (15-min plan step)
        ControlIntent(device_id="wallbox_a", timestep=now - timedelta(minutes=30),
                      power_kw=5.0, grid_allowed=False),
        # wallbox_b: holds the current slot
        ControlIntent(device_id="wallbox_b", timestep=now, power_kw=5.0, grid_allowed=False),
    ])

    setpoints = {d: w for d, w, _m in loop.describe_setpoints(_two_ev_live(now))}
    assert setpoints["wallbox_a"] == 0.0            # stale slot expired → yields
    assert 0.0 < setpoints["wallbox_b"] <= 500.0    # current holder keeps PV mode


def test_battery_leaves_reserved_surplus_for_planned_ev() -> None:
    """An idle battery must not absorb the surplus allocated to a PV-planned
    EV — openWB PV mode only starts charging when it sees grid export, so a
    battery grabbing it first would permanently starve the EV."""
    now = datetime.now(timezone.utc)
    from energy_assistant.core.models import EnergyPlan

    loop = ControlLoop(ledger=BatteryCostLedger())
    bat = StorageConstraints(
        device_id="bat", capacity_kwh=10.0, max_charge_kw=3.0,
        max_discharge_kw=3.0, min_soc_pct=10.0, max_soc_pct=95.0,
    )
    loop.register_contributor(StorageControlContributor(bat))
    ev_contrib = EvChargerContributor(_asset("wallbox"))
    ev_contrib.update_goal(build_goal_from_parts(
        asset_id="ev1", device_id="wallbox", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14, charge_limit_soc_pct=90.0,
        target_soc_pct=90.0, target_by=now + timedelta(hours=12),
        charge_curve=[], current_soc_pct=50.0, connected=True,
    ))
    loop.register_contributor(ev_contrib)
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="wallbox", timestep=now, power_kw=5.0, grid_allowed=False),
    ]))

    # Grid exporting 5 kW; EV not ramped up yet (0 W) → whole surplus reserved.
    live = _live(now, grid_power_w=-5_000.0, device_states={
        "bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0),
        "wallbox": DeviceState(device_id="wallbox", power_w=0.0, soc_pct=50.0, available=True),
    })
    setpoints = {d: w for d, w, _m in loop.describe_setpoints(live)}
    assert setpoints["bat"] is not None and setpoints["bat"] <= 1.0, (
        f"battery must not absorb the EV's reserved surplus, got {setpoints['bat']} W"
    )

    # EV ramped up to its 5 kW allocation; 1 kW genuine leftover exported.
    live2 = _live(now, grid_power_w=-1_000.0, device_states={
        "bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0),
        "wallbox": DeviceState(device_id="wallbox", power_w=5_000.0, soc_pct=50.0, available=True),
    })
    setpoints2 = {d: w for d, w, _m in loop.describe_setpoints(live2)}
    assert setpoints2["bat"] is not None and setpoints2["bat"] >= 900.0, (
        f"battery should absorb the genuine leftover, got {setpoints2['bat']} W"
    )


def test_grid_planned_ev_does_not_force_others_to_yield() -> None:
    """An instant/grid slot doesn't consume the PV surplus — the other EV
    may keep absorbing it opportunistically."""
    now = datetime.now(timezone.utc)
    loop, _ = _two_ev_loop(now, plan_intents=[
        ControlIntent(device_id="wallbox_a", timestep=now, power_kw=11.0, grid_allowed=True),
    ])

    setpoints = {d: w for d, w, _m in loop.describe_setpoints(_two_ev_live(now))}
    assert setpoints["wallbox_a"] == 11_000.0       # instant at planned power
    assert 0.0 < setpoints["wallbox_b"] <= 500.0    # opportunistic PV allowed


# ---------------------------------------------------------------------------
# OpenWBDevice — reconciliation (check current state before writing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openwb_skips_mode_write_when_already_in_desired_mode() -> None:
    client = FakeHAClient(states={"select.mode": "Stop"})
    dev = OpenWBDevice("wallbox", client, entity_mode="select.mode", entity_soc="sensor.soc")

    await dev.send_command(DeviceCommand(device_id="wallbox", command="set_power_w", value=0.0))

    assert client.calls == []


@pytest.mark.asyncio
async def test_openwb_writes_mode_on_mismatch() -> None:
    client = FakeHAClient(states={"select.mode": "Instant Charging"})
    dev = OpenWBDevice("wallbox", client, entity_mode="select.mode", entity_soc="sensor.soc")

    await dev.send_command(DeviceCommand(device_id="wallbox", command="set_power_w", value=0.0))

    assert client.calls == [
        ("select", "select_option", {"entity_id": "select.mode", "option": "Stop"}),
    ]


@pytest.mark.asyncio
async def test_openwb_writes_mode_when_current_mode_unreadable() -> None:
    client = FakeHAClient(states={})  # mode entity missing → read returns None
    dev = OpenWBDevice("wallbox", client, entity_mode="select.mode", entity_soc="sensor.soc")

    await dev.send_command(DeviceCommand(device_id="wallbox", command="set_power_w", value=1.0))

    assert client.calls == [
        ("select", "select_option", {"entity_id": "select.mode", "option": "PV Charging"}),
    ]


@pytest.mark.asyncio
async def test_openwb_skips_current_write_when_already_at_value() -> None:
    client = FakeHAClient(
        states={"select.mode": "Instant Charging", "number.current": "16"}
    )
    dev = OpenWBDevice(
        "wallbox", client,
        entity_mode="select.mode", entity_soc="sensor.soc",
        entity_current_instant="number.current",
    )

    # 11 kW / (230 V × 3) ≈ 16 A → matches the current value → no write at all.
    await dev.send_command(
        DeviceCommand(device_id="wallbox", command="set_power_w", value=11_000.0)
    )

    assert client.calls == []


@pytest.mark.asyncio
async def test_openwb_writes_current_on_mismatch() -> None:
    client = FakeHAClient(
        states={"select.mode": "Instant Charging", "number.current": "6"}
    )
    dev = OpenWBDevice(
        "wallbox", client,
        entity_mode="select.mode", entity_soc="sensor.soc",
        entity_current_instant="number.current",
    )

    await dev.send_command(
        DeviceCommand(device_id="wallbox", command="set_power_w", value=11_000.0)
    )

    assert client.calls == [
        ("number", "set_value", {"entity_id": "number.current", "value": 16.0}),
    ]


# ---------------------------------------------------------------------------
# ControlLoop — grid correction must ignore EV consumption
# ---------------------------------------------------------------------------


def test_grid_correction_excludes_ev_consumption() -> None:
    """EV charging power is real load — it must not be subtracted from the
    grid reading as if it were controllable battery charge power."""
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)

    bat = StorageConstraints(
        device_id="bat", capacity_kwh=10.0, max_charge_kw=3.0,
        max_discharge_kw=3.0, min_soc_pct=10.0, max_soc_pct=95.0,
    )
    loop.register_contributor(StorageControlContributor(bat))
    ev = EvChargerContributor(_asset("wallbox"))
    loop.register_contributor(ev)

    # EV instant-charging 11 kW from the grid, battery idle, no plan.
    # If the EV power were (wrongly) removed, effective grid would look like
    # -10 kW of PV surplus and the battery would be told to charge at 3 kW.
    live = _live(
        now,
        grid_power_w=11_000.0,
        device_states={
            "bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0),
            "wallbox": DeviceState(
                device_id="wallbox", power_w=11_000.0, soc_pct=40.0, available=True
            ),
        },
    )

    setpoints = {d: (w, m) for d, w, m in loop.describe_setpoints(live)}
    bat_w, _ = setpoints["bat"]
    assert bat_w is not None
    assert bat_w <= 0.0, f"battery must not charge from phantom surplus, got {bat_w} W"

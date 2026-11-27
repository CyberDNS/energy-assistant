from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from nornwatt.assets.threshold import ThresholdControlContributor
from nornwatt.core.control import (
    ControlLoop,
    LiveSituation,
    StorageControlContributor,
)
from nornwatt.core.device_model import DeviceCategory
from nornwatt.core.ledger import BatteryCostLedger
from nornwatt.core.models import (
    ControlIntent,
    DeviceCommand,
    DeviceState,
    EnergyPlan,
    StorageConstraints,
    ThresholdConstraints,
)
from nornwatt.core.realtime_slice_optimizer import (
    StorageSliceInput,
    optimize_storage_slice,
)
from nornwatt.core.registry import DeviceRegistry


class _FakeStorageDevice:
    def __init__(self, device_id: str) -> None:
        self._device_id = device_id
        self._category = DeviceCategory.BATTERY
        self.commands: list[DeviceCommand] = []

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return self._category

    async def get_state(self) -> DeviceState:
        return DeviceState(device_id=self._device_id, power_w=0.0)

    async def send_command(self, command: DeviceCommand) -> None:
        self.commands.append(command)


def _constraints(device_id: str = "bat") -> StorageConstraints:
    return StorageConstraints(
        device_id=device_id,
        capacity_kwh=10.0,
        max_charge_kw=3.0,
        max_discharge_kw=3.0,
        min_soc_pct=10.0,
        max_soc_pct=95.0,
    )


@pytest.mark.asyncio
async def test_pv_only_charge_is_capped_to_live_surplus() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    sc = _constraints("bat")
    sc.no_grid_charge = True
    loop.register_contributor(StorageControlContributor(sc))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=3.0,
                    grid_allowed=False,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=-1200.0,  # exporting 1.2 kW surplus
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.25,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    await loop.tick(live, registry)

    assert dev.commands
    assert dev.commands[-1].command == "set_power_w"
    assert dev.commands[-1].value == 1200.0


@pytest.mark.asyncio
async def test_discharge_meet_load_only_caps_to_import() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=1000.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.25,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    await loop.tick(live, registry)

    assert dev.commands
    assert dev.commands[-1].value == -1000.0


@pytest.mark.asyncio
async def test_discharge_export_allowed_when_profitable() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.05)
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                    export_allowed=True,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=1000.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.25,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    await loop.tick(live, registry)

    assert dev.commands
    assert dev.commands[-1].value == -3000.0


@pytest.mark.asyncio
async def test_discharge_mode_may_absorb_pv_surplus() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=-900.0,  # exporting PV surplus
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.35,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    await loop.tick(live, registry)

    assert dev.commands
    # Planner says discharge, but realtime layer may opportunistically charge on PV surplus.
    assert dev.commands[-1].value > 0.0


@pytest.mark.asyncio
async def test_idle_auto_absorbs_pv_surplus() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=0.0,
                    grid_allowed=False,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=-800.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.25,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    await loop.tick(live, registry)

    assert dev.commands
    assert float(dev.commands[-1].value) > 0.0


def test_describe_setpoints_discharge_with_zero_dt() -> None:
    """Status-preview path (dt=0) should still suggest discharge under import."""
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                )
            ]
        )
    )

    live = LiveSituation(
        timestamp=now,
        grid_power_w=350.0,
        dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.22,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    rows = loop.describe_setpoints(live)
    assert rows
    _did, setpoint_w, _mode = rows[0]
    assert setpoint_w is not None
    assert setpoint_w < 0.0


def test_describe_setpoints_uses_default_zone_grid_reference() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                )
            ]
        )
    )

    live = LiveSituation(
        timestamp=now,
        grid_power_w=250.0,
        default_zone_grid_power_w=600.0,
        dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.22,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    rows = loop.describe_setpoints(live)
    assert rows
    _did, setpoint_w, _mode = rows[0]
    assert setpoint_w is not None
    assert setpoint_w <= -550.0


def test_describe_setpoints_uses_existing_discharge_headroom() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                )
            ]
        )
    )

    live = LiveSituation(
        timestamp=now,
        grid_power_w=0.0,
        dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=-300.0)},
        current_price_eur_per_kwh=0.22,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    rows = loop.describe_setpoints(live)
    assert rows
    _did, setpoint_w, _mode = rows[0]
    assert setpoint_w is not None
    assert setpoint_w < 0.0


def test_describe_setpoints_discharge_scales_with_import() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                )
            ]
        )
    )

    live_low = LiveSituation(
        timestamp=now,
        grid_power_w=250.0,
        dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.22,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    low_rows = loop.describe_setpoints(live_low)
    low_sp = float(low_rows[0][1] or 0.0)

    live_high = LiveSituation(
        timestamp=now,
        grid_power_w=700.0,
        dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.22,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    high_rows = loop.describe_setpoints(live_high)
    high_sp = float(high_rows[0][1] or 0.0)

    assert low_sp < 0.0
    assert high_sp < low_sp


@pytest.mark.asyncio
async def test_discharge_does_not_jojo_to_zero_on_next_tick() -> None:
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-3.0,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    # Tick 1: clear import, expect discharge command.
    live1 = LiveSituation(
        timestamp=now,
        grid_power_w=300.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.22,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    await loop.tick(live1, registry)
    first = float(dev.commands[-1].value)
    assert first < 0.0

    # Tick 2: import measured near zero should not force immediate 0W hold.
    live2 = LiveSituation(
        timestamp=now,
        grid_power_w=0.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.22,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    await loop.tick(live2, registry)
    second = float(dev.commands[-1].value)
    assert second < 0.0


@pytest.mark.asyncio
async def test_charge_from_grid_follows_planned_power() -> None:
    """charge_from_grid mode should command the full planned power (may draw from grid)."""
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=2.0,
                    grid_allowed=True,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=500.0,   # importing: no PV surplus, grid covers the rest
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.10,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    await loop.tick(live, registry)

    assert dev.commands
    assert dev.commands[-1].value > 0.0


@pytest.mark.asyncio
async def test_grid_feed_in_allows_export() -> None:
    """grid_feed_in mode should permit discharge past zero (export to grid)."""
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    ledger.initialise("bat", stored_energy_kwh=5.0, cost_basis_eur_per_kwh=0.05)
    loop = ControlLoop(ledger=ledger)
    loop.register_contributor(StorageControlContributor(_constraints("bat")))

    loop.update_plan(
        EnergyPlan(
            intents=[
                ControlIntent(
                    device_id="bat",
                    timestep=now,
                    power_kw=-2.0,
                    export_allowed=True,
                )
            ]
        )
    )

    registry = DeviceRegistry()
    dev = _FakeStorageDevice("bat")
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=-200.0,  # already slightly exporting PV — battery adds to it
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0)},
        current_price_eur_per_kwh=0.25,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    await loop.tick(live, registry)

    assert dev.commands
    assert dev.commands[-1].value < 0.0   # discharging — pushing energy to grid


class _FakeThresholdDevice:
    def __init__(self, device_id: str, power_w: float) -> None:
        self._device_id = device_id
        self._power_w = power_w
        self.commands: list[DeviceCommand] = []

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.THERMAL_LOAD

    async def get_state(self) -> DeviceState:
        return DeviceState(device_id=self._device_id, power_w=self._power_w)

    async def send_command(self, command: DeviceCommand) -> None:
        self.commands.append(command)


@pytest.mark.asyncio
async def test_tick_before_first_plan_sends_no_commands() -> None:
    """A tick that arrives before the optimizer's first plan must be a no-op.

    Regression test: on startup the control loop can tick before the first
    plan is solved. With no active plan, every contributor resolves
    ``intent=None`` — a threshold device would fall back to its "standby"
    default and send a real turn_off, which also arms the compressor
    min-offtime lockout and keeps the device off for up to ``min_offtime_h``
    even after the real plan (saying "run") lands moments later.
    """
    now = datetime.now(timezone.utc)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    tc = ThresholdConstraints(
        device_id="cooler",
        bottom_threshold=18.0,
        top_threshold=20.5,
        unit="°C",
        direction="reduces",
        rated_power_kw=0.15,
        active_rate_per_h=0.417,
        drift_rate_per_h=0.185,
        min_runtime_h=0.5,
        min_offtime_h=0.5,
    )
    loop.register_contributor(ThresholdControlContributor(tc))

    registry = DeviceRegistry()
    # Device is currently running (e.g. left on from before a restart).
    dev = _FakeThresholdDevice("cooler", power_w=150.0)
    registry.register(dev)

    live = LiveSituation(
        timestamp=now,
        grid_power_w=0.0,
        dt_hours=5.0 / 3600.0,
        device_states={
            "cooler": DeviceState(
                device_id="cooler",
                power_w=150.0,
                extra={"measured_value": 19.7},  # comfortably within bounds
            )
        },
        current_price_eur_per_kwh=0.25,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    # No update_plan() call — this is the pre-first-plan startup window.
    await loop.tick(live, registry)

    assert dev.commands == []


# ── Plan fidelity: idle holds, discharge stays within the plan ────────────────


def _single_intent_loop(now: datetime, power_kw: float) -> ControlLoop:
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(_constraints("bat")))
    loop.update_plan(
        EnergyPlan(intents=[ControlIntent(device_id="bat", timestep=now, power_kw=power_kw)])
    )
    return loop


@pytest.mark.parametrize("price", [0.30, 0.46, 0.53])
@pytest.mark.parametrize("current_w", [0.0, -600.0])
def test_idle_holds_at_high_import_price(price: float, current_w: float) -> None:
    """Idle means "keep the energy for later", even when import is expensive
    and the battery was discharging a moment ago (e.g. a new plan arrived)."""
    now = datetime.now(timezone.utc)
    loop = _single_intent_loop(now, 0.0)
    live = LiveSituation(
        timestamp=now,
        grid_power_w=700.0 + current_w,  # 700 W house load, battery covers |current_w|
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=current_w, soc_pct=50.0)},
        current_price_eur_per_kwh=price,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    (_did, setpoint_w, mode), = loop.describe_setpoints(live)
    assert mode == "idle"
    assert setpoint_w == 0.0


def test_no_plan_slot_holds_at_high_import_price() -> None:
    now = datetime.now(timezone.utc)
    loop = _single_intent_loop(now - timedelta(hours=1), -1.0)  # intent long expired
    live = LiveSituation(
        timestamp=now,
        grid_power_w=700.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0)},
        current_price_eur_per_kwh=0.50,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    (_did, setpoint_w, _mode), = loop.describe_setpoints(live)
    assert setpoint_w == 0.0


def test_small_planned_discharge_is_not_turned_into_full_load_following() -> None:
    now = datetime.now(timezone.utc)
    loop = _single_intent_loop(now, -0.2)
    live = LiveSituation(
        timestamp=now,
        grid_power_w=700.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0)},
        current_price_eur_per_kwh=0.50,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    (_did, setpoint_w, _mode), = loop.describe_setpoints(live)
    assert setpoint_w is not None
    assert -250.0 - 1e-6 <= setpoint_w < -150.0


def _valued_discharge_loop(now: datetime, power_kw: float, value: float | None) -> ControlLoop:
    loop = ControlLoop(ledger=BatteryCostLedger())
    sc = _constraints("bat")
    sc.discharge_efficiency = 0.9
    loop.register_contributor(StorageControlContributor(sc))
    loop.update_plan(EnergyPlan(intents=[ControlIntent(
        device_id="bat", timestep=now, power_kw=power_kw, energy_value_eur_per_kwh=value,
    )]))
    return loop


def _evening(now: datetime, price: float) -> LiveSituation:
    return LiveSituation(
        timestamp=now,
        grid_power_w=700.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0)},
        current_price_eur_per_kwh=price,
        pv_opportunity_price_eur_per_kwh=0.08,
    )


def test_small_discharge_covers_unforecast_load_when_import_beats_its_value() -> None:
    """The plan values the stored kWh at 0.43 (0.48 delivered at η 0.9); at
    0.62 import an unforecast 700 W load is cheaper from the battery."""
    now = datetime.now(UTC)
    loop = _valued_discharge_loop(now, -0.1, 0.43)
    (_did, setpoint_w, _mode), = loop.describe_setpoints(_evening(now, 0.62))
    assert setpoint_w is not None
    assert -700.0 - 1e-6 <= setpoint_w <= -550.0  # ≈ the load, no export


@pytest.mark.parametrize("value", [None, 0.55])
def test_small_discharge_keeps_its_headroom_when_the_energy_is_worth_more(value) -> None:
    # 0.55 / 0.9 = 0.61 delivered > 0.50 import: keep it for the plan's later use
    now = datetime.now(UTC)
    loop = _valued_discharge_loop(now, -0.2, value)
    (_did, setpoint_w, _mode), = loop.describe_setpoints(_evening(now, 0.50))
    assert setpoint_w is not None
    assert -250.0 - 1e-6 <= setpoint_w < -150.0


def test_unforecast_load_is_covered_close_to_break_even() -> None:
    """Seen on the sim stack: 0.4337 / 0.95 = 0.457 delivered vs 0.5015 import
    — the tracking and slew tie-breakers must not keep the battery capped."""
    out = optimize_storage_slice(
        [StorageSliceInput(device_id="bat", max_charge_w=2500, max_discharge_w=2500,
                           no_grid_charge=False, export_allowed=False, planned_w=-882,
                           reserved_kwh=0.0, prev_setpoint_w=-1102.5,
                           energy_value_eur_per_kwh=0.4337, discharge_efficiency=0.95)],
        grid_power_w=1618.0, pv_surplus_w=0.0, dt_hours=5 / 3600,
        import_price_eur_per_kwh=0.5015, export_price_eur_per_kwh=0.08,
        cost_basis_eur_per_kwh={},
    )
    assert out is not None
    assert out["bat"] == pytest.approx(-1618.0, abs=5.0)


def test_unforecast_load_goes_to_the_battery_whose_energy_is_worth_least() -> None:
    """Both small discharges may cover the spike; the cheaper energy goes first."""
    def inp(did: str, value: float) -> StorageSliceInput:
        return StorageSliceInput(
            device_id=did, max_charge_w=3000, max_discharge_w=3000, no_grid_charge=False,
            export_allowed=False, planned_w=-100, reserved_kwh=0.0,
            energy_value_eur_per_kwh=value, discharge_efficiency=0.9,
        )
    out = optimize_storage_slice(
        [inp("dear", 0.50), inp("cheap", 0.40)],
        grid_power_w=1500.0, pv_surplus_w=0.0, dt_hours=5 / 3600,
        import_price_eur_per_kwh=0.62, export_price_eur_per_kwh=0.08,
        cost_basis_eur_per_kwh={},
    )
    assert out is not None
    assert out["cheap"] <= -1200.0
    assert -125.0 - 1e-6 <= out["dear"] <= 0.0
    assert out["cheap"] + out["dear"] >= -1500.0 - 1e-6  # no export


def test_tonights_case_small_battery_covers_what_the_capped_one_cannot() -> None:
    """Zendure-like battery planned at its 800 W limit, SMA-like battery at a
    106 W share; a 1.5 kW load spike is covered by the SMA instead of the grid."""
    out = optimize_storage_slice(
        [
            StorageSliceInput(device_id="zendure", max_charge_w=1200, max_discharge_w=800,
                              no_grid_charge=False, export_allowed=False, planned_w=-800,
                              reserved_kwh=0.0, energy_value_eur_per_kwh=0.43,
                              discharge_efficiency=0.9),
            StorageSliceInput(device_id="sma", max_charge_w=3000, max_discharge_w=3000,
                              no_grid_charge=False, export_allowed=False, planned_w=-106,
                              reserved_kwh=0.0, energy_value_eur_per_kwh=0.43,
                              discharge_efficiency=0.9),
        ],
        grid_power_w=1500.0, pv_surplus_w=0.0, dt_hours=5 / 3600,
        import_price_eur_per_kwh=0.62, export_price_eur_per_kwh=0.08,
        cost_basis_eur_per_kwh={},
    )
    assert out is not None
    assert out["zendure"] == pytest.approx(-800.0, abs=1.0)
    assert out["sma"] == pytest.approx(-700.0, abs=5.0)


def test_planned_discharge_still_follows_lower_load() -> None:
    now = datetime.now(timezone.utc)
    loop = _single_intent_loop(now, -1.0)
    live = LiveSituation(
        timestamp=now,
        grid_power_w=400.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0)},
        current_price_eur_per_kwh=0.50,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    (_did, setpoint_w, _mode), = loop.describe_setpoints(live)
    assert setpoint_w is not None
    assert -520.0 <= setpoint_w <= -300.0  # ≈ load (+ small import buffer), no export


def test_pv_only_charge_without_surplus_does_not_discharge() -> None:
    now = datetime.now(timezone.utc)
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(_constraints("bat")))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="bat", timestep=now, power_kw=1.0, grid_allowed=False)
    ]))
    live = LiveSituation(
        timestamp=now,
        grid_power_w=700.0,
        dt_hours=5.0 / 3600.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=0.0, soc_pct=50.0)},
        current_price_eur_per_kwh=0.50,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    (_did, setpoint_w, _mode), = loop.describe_setpoints(live)
    assert setpoint_w == 0.0


# ── Plan fidelity: planned grid charging, shared PV pool (#75) ────────────────


def _two_battery_loop(now: datetime, intents: list[ControlIntent]) -> ControlLoop:
    """``pv`` can only charge from PV (``no_grid_charge``), ``ac`` may use the grid."""
    loop = ControlLoop(ledger=BatteryCostLedger())
    pv = _constraints("pv")
    pv.no_grid_charge = True
    loop.register_contributor(StorageControlContributor(pv))
    loop.register_contributor(StorageControlContributor(_constraints("ac")))
    loop.update_plan(EnergyPlan(intents=intents))
    return loop


def _live(now: datetime, grid_power_w: float, price: float, *device_ids: str) -> LiveSituation:
    return LiveSituation(
        timestamp=now,
        grid_power_w=grid_power_w,
        dt_hours=5.0 / 3600.0,
        device_states={
            did: DeviceState(device_id=did, power_w=0.0, soc_pct=50.0) for did in device_ids
        },
        current_price_eur_per_kwh=price,
        pv_opportunity_price_eur_per_kwh=0.08,
    )


def _setpoints(loop: ControlLoop, live: LiveSituation) -> dict[str, float | None]:
    return {did: sp for did, sp, _mode in loop.describe_setpoints(live)}


@pytest.mark.parametrize("price", [0.10, 0.30, 0.53])
def test_grid_charge_delivers_planned_power_at_any_import_price(price: float) -> None:
    """The planner already priced this slot; the realtime layer must not cut
    planned grid charging when import is expensive (was: 60 % floor > 0.2 €/kWh)."""
    now = datetime.now(timezone.utc)
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(_constraints("bat")))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="bat", timestep=now, power_kw=1.2, grid_allowed=True)
    ]))
    assert _setpoints(loop, _live(now, 700.0, price, "bat"))["bat"] == 1200.0


def test_grid_charge_is_limited_by_device_max_and_soc() -> None:
    now = datetime.now(timezone.utc)
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(_constraints("bat")))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="bat", timestep=now, power_kw=5.0, grid_allowed=True)
    ]))
    live = _live(now, 700.0, 0.30, "bat")
    assert _setpoints(loop, live)["bat"] == 3000.0  # max_charge_kw

    live.device_states["bat"] = DeviceState(device_id="bat", power_w=0.0, soc_pct=95.0)
    assert _setpoints(loop, live)["bat"] == 0.0  # at max SoC


def test_scarce_pv_goes_to_the_pv_only_battery_first() -> None:
    """Surplus below the PV-only plan: the PV-only battery takes all of it,
    the grid-capable battery meets its plan from the grid."""
    now = datetime.now(timezone.utc)
    loop = _two_battery_loop(now, [
        ControlIntent(device_id="pv", timestep=now, power_kw=1.0, grid_allowed=False),
        ControlIntent(device_id="ac", timestep=now, power_kw=1.2, grid_allowed=True),
    ])
    sp = _setpoints(loop, _live(now, -800.0, 0.30, "pv", "ac"))
    assert sp == {"pv": 800.0, "ac": 1200.0}


def test_pv_left_after_the_pv_only_plan_offsets_grid_charging() -> None:
    """Surplus above the PV-only plan: both plans are met, the remainder
    reduces the grid-capable battery's import and nothing is exported."""
    now = datetime.now(timezone.utc)
    loop = _two_battery_loop(now, [
        ControlIntent(device_id="pv", timestep=now, power_kw=1.0, grid_allowed=False),
        ControlIntent(device_id="ac", timestep=now, power_kw=1.2, grid_allowed=True),
    ])
    sp = _setpoints(loop, _live(now, -1500.0, 0.30, "pv", "ac"))
    assert sp["pv"] >= 1000.0 - 1e-6
    assert sp["ac"] >= 1200.0 - 1e-6
    assert sp["pv"] + sp["ac"] == pytest.approx(2200.0)  # 700 W from the grid, no extra


def test_surplus_beyond_all_plans_is_absorbed_not_exported() -> None:
    now = datetime.now(timezone.utc)
    loop = _two_battery_loop(now, [
        ControlIntent(device_id="pv", timestep=now, power_kw=1.0, grid_allowed=False),
        ControlIntent(device_id="ac", timestep=now, power_kw=1.2, grid_allowed=True),
    ])
    sp = _setpoints(loop, _live(now, -4000.0, 0.30, "pv", "ac"))
    assert sp["pv"] >= 1000.0 - 1e-6
    assert sp["ac"] >= 1200.0 - 1e-6
    assert sp["pv"] + sp["ac"] == pytest.approx(4000.0)


def test_idle_pv_only_battery_absorbs_surplus_the_plan_leaves() -> None:
    """Grid-capable battery charging from the grid, PV-only battery idle:
    the surplus first serves the planned charge, the idle battery only gets
    what is left (it does not make the other battery import more)."""
    now = datetime.now(timezone.utc)
    loop = _two_battery_loop(now, [
        ControlIntent(device_id="pv", timestep=now, power_kw=0.0),
        ControlIntent(device_id="ac", timestep=now, power_kw=1.2, grid_allowed=True),
    ])
    sp = _setpoints(loop, _live(now, -2000.0, 0.30, "pv", "ac"))
    assert sp["ac"] >= 1200.0 - 1e-6
    assert sp["pv"] + sp["ac"] == pytest.approx(2000.0)

    loop = _two_battery_loop(now, [
        ControlIntent(device_id="pv", timestep=now, power_kw=0.0),
        ControlIntent(device_id="ac", timestep=now, power_kw=1.2, grid_allowed=True),
    ])
    sp = _setpoints(loop, _live(now, -800.0, 0.30, "pv", "ac"))
    assert sp == {"pv": 0.0, "ac": 1200.0}


def test_two_pv_only_batteries_share_the_surplus() -> None:
    """The surplus is one pool: two PV-only plans cannot both take all of it."""
    now = datetime.now(timezone.utc)
    loop = ControlLoop(ledger=BatteryCostLedger())
    for did in ("a", "b"):
        sc = _constraints(did)
        sc.no_grid_charge = True
        loop.register_contributor(StorageControlContributor(sc))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="a", timestep=now, power_kw=1.0, grid_allowed=False),
        ControlIntent(device_id="b", timestep=now, power_kw=1.0, grid_allowed=False),
    ]))
    sp = _setpoints(loop, _live(now, -1200.0, 0.30, "a", "b"))
    assert sp["a"] + sp["b"] == pytest.approx(1200.0)


def test_discharge_does_not_feed_another_batterys_grid_charging() -> None:
    """A planned discharge covers the house load only, not a parallel grid charge."""
    now = datetime.now(timezone.utc)
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(StorageControlContributor(_constraints("dis")))
    loop.register_contributor(StorageControlContributor(_constraints("chg")))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id="dis", timestep=now, power_kw=-2.0),
        ControlIntent(device_id="chg", timestep=now, power_kw=1.5, grid_allowed=True),
    ]))
    sp = _setpoints(loop, _live(now, 500.0, 0.30, "dis", "chg"))
    assert sp["chg"] == 1500.0
    assert -500.0 - 1e-6 <= sp["dis"] <= 0.0


class _FailingStorageDevice(_FakeStorageDevice):
    def __init__(self, device_id: str) -> None:
        super().__init__(device_id)
        self.fail = True

    async def send_command(self, command: DeviceCommand) -> None:
        if self.fail:
            raise RuntimeError("write holding 40793 rejected")
        await super().send_command(command)


@pytest.mark.asyncio
async def test_a_failing_device_does_not_block_the_others(caplog) -> None:
    import logging

    now = datetime.now(UTC)
    ledger = BatteryCostLedger()
    loop = ControlLoop(ledger=ledger)
    for device_id in ("bad", "good"):
        loop.register_contributor(StorageControlContributor(_constraints(device_id)))
    loop.update_plan(EnergyPlan(intents=[
        ControlIntent(device_id=d, timestep=now, power_kw=1.0) for d in ("bad", "good")
    ]))
    registry = DeviceRegistry()
    bad, good = _FailingStorageDevice("bad"), _FakeStorageDevice("good")
    registry.register(bad)
    registry.register(good)
    live = LiveSituation(
        timestamp=now,
        grid_power_w=0.0,
        dt_hours=1.0,
        device_states={d: DeviceState(device_id=d, power_w=1000.0) for d in ("bad", "good")},
        current_price_eur_per_kwh=0.25,
        pv_opportunity_price_eur_per_kwh=0.08,
    )

    with caplog.at_level(logging.INFO, logger="nornwatt.core.control"):
        await loop.tick(live, registry)
        await loop.tick(live, registry)
        bad.fail = False
        await loop.tick(live, registry)

    # The device after the failing one is still commanded on every tick …
    assert [c.value for c in good.commands] == [1000.0, 1000.0, 1000.0]
    # … the failing one again on each tick, and it gets through once it works.
    assert [c.value for c in bad.commands] == [1000.0]
    # The ledger still follows both batteries' measured power.
    assert ledger.stored_energy("bad") == pytest.approx(ledger.stored_energy("good"))
    assert ledger.stored_energy("bad") > 0
    messages = [r.getMessage() for r in caplog.records]
    assert sum("command to 'bad' failed" in m for m in messages) == 1  # warned once
    assert any("commands to 'bad' work again" in m for m in messages)

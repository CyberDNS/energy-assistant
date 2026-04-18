from __future__ import annotations

from datetime import datetime, timezone

import pytest

from energy_assistant.core.control import ControlLoop, LiveSituation, StorageControlContributor
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import ControlIntent, DeviceCommand, DeviceRole, DeviceState, EnergyPlan, StorageConstraints
from energy_assistant.core.registry import DeviceRegistry


class _FakeStorageDevice:
    def __init__(self, device_id: str) -> None:
        self._device_id = device_id
        self._role = DeviceRole.STORAGE
        self.commands: list[DeviceCommand] = []

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

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
                    mode="charge_from_pv",
                    min_power_w=0.0,
                    max_power_w=3000.0,
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    discharge_policy="meet_load_only",
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    discharge_policy="allow_export_if_profitable",
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    planned_kw=-3.0,
                    discharge_policy="meet_load_only",
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
                    mode="charge_from_pv",
                    min_power_w=0.0,
                    max_power_w=0.0,
                    charge_policy="auto",
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    discharge_policy="meet_load_only",
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    discharge_policy="meet_load_only",
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    discharge_policy="meet_load_only",
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    planned_kw=-0.2,
                    discharge_policy="meet_load_only",
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
                    mode="discharge",
                    min_power_w=-3000.0,
                    max_power_w=0.0,
                    discharge_policy="meet_load_only",
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
                    mode="charge_from_grid",
                    min_power_w=0.0,
                    max_power_w=2000.0,
                    planned_kw=2.0,
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
                    mode="grid_feed_in",
                    min_power_w=-2000.0,
                    max_power_w=0.0,
                    planned_kw=-2.0,
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

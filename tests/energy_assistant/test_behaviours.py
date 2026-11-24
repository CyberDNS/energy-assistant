"""Behaviours: what a battery or charger setpoint is for (#106 phase 3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from energy_assistant.core.control import (
    ControlLoop,
    LiveSituation,
    StorageControlContributor,
)
from energy_assistant.core.device_model import DeviceCategory
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import (
    Behaviour,
    ControlIntent,
    DeviceCommand,
    DeviceState,
    EnergyPlan,
    StorageConstraints,
    charger_behaviour,
    resolve_behaviour,
)
from energy_assistant.core.registry import DeviceRegistry

B = Behaviour
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def test_resolve_behaviour_falls_back_to_the_forced_one() -> None:
    assert resolve_behaviour(B.SELF_CONSUMPTION, -800.0, None) is B.SELF_CONSUMPTION
    forced = frozenset({B.CHARGE, B.DISCHARGE, B.HOLD})
    assert resolve_behaviour(B.SELF_CONSUMPTION, -800.0, forced) is B.DISCHARGE
    assert resolve_behaviour(B.SELF_CONSUMPTION, 300.0, forced) is B.CHARGE
    assert resolve_behaviour(B.ABSORB_SURPLUS, 0.0, forced) is B.HOLD
    assert resolve_behaviour(B.ABSORB_SURPLUS, 0.0, frozenset({B.CHARGE, B.STOP})) is B.STOP
    assert resolve_behaviour(B.ABSORB_SURPLUS, -5.0, frozenset({B.CHARGE})) is None
    assert resolve_behaviour(None, 100.0, forced) is None


def test_charger_behaviour_without_a_behaviour_has_no_watt_encoding() -> None:
    def cmd(value, behaviour=None):
        return DeviceCommand(device_id="wb", command="set_power_w", value=value,
                             behaviour=behaviour)

    assert charger_behaviour(cmd(1.0)) is B.CHARGE       # 1 W is no longer "PV mode"
    assert charger_behaviour(cmd(0.0)) is B.STOP
    assert charger_behaviour(cmd(0.0, B.ABSORB_SURPLUS)) is B.ABSORB_SURPLUS
    assert charger_behaviour(cmd(0.0, B.HOLD)) is B.STOP


def _live(grid_w: float = 0.0, power_w: float = 0.0) -> LiveSituation:
    return LiveSituation(
        timestamp=NOW, grid_power_w=grid_w, dt_hours=0.0,
        device_states={"bat": DeviceState(device_id="bat", power_w=power_w, soc_pct=50.0)},
        current_price_eur_per_kwh=0.30, pv_opportunity_price_eur_per_kwh=0.08,
    )


def _storage(**kw) -> StorageControlContributor:
    return StorageControlContributor(StorageConstraints(
        device_id="bat", capacity_kwh=10.0, max_charge_kw=3.0, max_discharge_kw=3.0, **kw))


def _intent(kw: float, **flags) -> ControlIntent:
    return ControlIntent(device_id="bat", timestep=NOW, power_kw=kw, **flags)


@pytest.mark.parametrize(("intent", "setpoint", "expected"), [
    (_intent(2.0), 2000.0, B.CHARGE),
    (_intent(2.0), 0.0, B.HOLD),                                # nothing to charge (full)
    (_intent(2.0, grid_allowed=False), 800.0, B.ABSORB_SURPLUS),  # PV-only charge
    (_intent(-1.5), -900.0, B.SELF_CONSUMPTION),                # follow the load
    (_intent(-1.5), 0.0, B.SELF_CONSUMPTION),
    (_intent(-1.5, export_allowed=True), -1500.0, B.DISCHARGE),  # forced, may export
    (_intent(0.0), 1200.0, B.ABSORB_SURPLUS),                   # idle: surplus only
    (None, 0.0, B.ABSORB_SURPLUS),                              # no plan
    (_intent(0.0), None, None),                                 # no command
])
def test_storage_behaviours(intent, setpoint, expected) -> None:
    assert _storage().behaviour_for(setpoint, intent, _live()) is expected


def test_storage_behaviour_when_curtailed_or_pv_only() -> None:
    assert _storage().behaviour_for(1000.0, _intent(3.0), _live(), curtailed=True) is B.CHARGE
    assert _storage().behaviour_for(0.0, _intent(3.0), _live(), curtailed=True) is B.HOLD
    assert _storage(no_grid_charge=True).behaviour_for(
        2000.0, _intent(2.0), _live()) is B.ABSORB_SURPLUS


class _Battery:
    category = DeviceCategory.BATTERY

    def __init__(self, supported: frozenset[Behaviour] | None = None) -> None:
        self.device_id = "bat"
        self.commands: list[DeviceCommand] = []
        if supported is not None:
            self.supported_behaviours = supported

    async def get_state(self) -> DeviceState:
        return DeviceState(device_id="bat", power_w=0.0)

    async def send_command(self, command: DeviceCommand) -> None:
        self.commands.append(command)


@pytest.mark.parametrize(("supported", "expected"), [
    (None, B.SELF_CONSUMPTION),
    (frozenset({B.CHARGE, B.DISCHARGE, B.HOLD}), B.DISCHARGE),
])
async def test_tick_sends_the_behaviour_the_device_supports(supported, expected) -> None:
    loop = ControlLoop(ledger=BatteryCostLedger())
    loop.register_contributor(_storage())
    loop.update_plan(EnergyPlan(intents=[_intent(-1.0)]))
    registry = DeviceRegistry()
    battery = _Battery(supported)
    registry.register(battery)

    await loop.tick(_live(grid_w=700.0), registry)   # 700 W import to cover

    (command,) = battery.commands
    assert command.value == pytest.approx(-700.0)
    assert command.behaviour is expected
    assert loop.last_behaviours == {"bat": B.SELF_CONSUMPTION}

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
                    mode="grid_fill",
                    min_power_w=0.0,
                    max_power_w=3000.0,
                    charge_policy="pv_only",
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

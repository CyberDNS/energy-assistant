"""Grid connection limit: topology config, realtime curtailment, planner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.assets.ev import EvChargerContributor, EvChargingAsset
from energy_assistant.core.control import ControlLoop, LiveSituation, StorageControlContributor
from energy_assistant.core.grid_limit import GridLimit
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import (
    ControlIntent,
    DeviceState,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
)
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.core.topology import build_topology
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer

_NOW = datetime(2026, 11, 18, 3, 5, tzinfo=timezone.utc)
_SLOT = _NOW.replace(minute=0)


def _storage(did: str) -> StorageControlContributor:
    return StorageControlContributor(StorageConstraints(
        device_id=did, capacity_kwh=10.0, max_charge_kw=3.0,
        max_discharge_kw=3.0, min_soc_pct=10.0, max_soc_pct=95.0,
    ))


class _FakeEv:
    """Minimal EV-like contributor: instant charging at a fixed setpoint."""

    is_ev = True

    def __init__(self, device_id: str, setpoint_w: float, min_w: float = 4140.0) -> None:
        self._id = device_id
        self._sp = setpoint_w
        self._min_w = min_w

    @property
    def device_id(self) -> str:
        return self._id

    def desired_setpoint_w(self, intent, live):  # noqa: ANN001
        return self._sp

    def charge_price_eur_per_kwh(self, intent, live):  # noqa: ANN001
        return live.current_price_eur_per_kwh

    def expected_draw_w(self, setpoint_w, live):  # noqa: ANN001
        return setpoint_w if setpoint_w > 500.0 else (0.0 if setpoint_w <= 0 else None)

    def curtail_setpoint_w(self, setpoint_w, reduce_by_w, live):  # noqa: ANN001
        new = setpoint_w - reduce_by_w
        return new if new >= self._min_w else 0.0


def _loop(contributors: list, intents: list[ControlIntent], limit: GridLimit | None) -> ControlLoop:
    loop = ControlLoop(ledger=BatteryCostLedger(), grid_limit=limit)
    for c in contributors:
        loop.register_contributor(c)
    loop.update_plan(EnergyPlan(created_at=_SLOT, intents=intents))
    return loop


def _charge(did: str, kw: float) -> ControlIntent:
    return ControlIntent(device_id=did, timestep=_SLOT, power_kw=kw, grid_allowed=True,
                         reserved_kwh=kw * 0.25)


def _sp(loop: ControlLoop, base_w: float, measured: dict[str, float]) -> dict[str, float | None]:
    """One preview tick: ``base_w`` is the house load without the controlled devices."""
    live = LiveSituation(
        timestamp=_NOW,
        grid_power_w=base_w + sum(measured.values()),
        dt_hours=0.0,   # previews: keep slot-energy tracking out of these tests
        device_states={
            did: DeviceState(device_id=did, power_w=w, soc_pct=50.0) for did, w in measured.items()
        },
        current_price_eur_per_kwh=0.30,
        pv_opportunity_price_eur_per_kwh=0.08,
    )
    return {did: sp for did, sp, _m in loop.describe_setpoints(live)}


# ── Topology config ───────────────────────────────────────────────────────────


def test_topology_parses_connection_limit() -> None:
    root = build_topology([{"grid": {
        "max_import_kw": 24, "curtail_order": ["wb", "bat"], "limit_hysteresis_kw": 0.5,
        "children": ["house"],
    }}])
    assert root is not None
    assert root.max_import_kw == 24.0
    assert root.curtail_order == ["wb", "bat"]
    assert root.limit_hysteresis_kw == 0.5
    assert [c.device_id for c in root.children] == ["house"]


def test_topology_without_limit_has_none() -> None:
    root = build_topology([{"grid": {"children": ["house"]}}])
    assert root is not None and root.max_import_kw is None and root.curtail_order == []


def test_topology_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        build_topology([{"grid": {"max_import_kw": 0}}])


def test_default_hysteresis_is_five_percent() -> None:
    assert GridLimit(max_import_w=20_000.0).release_w == pytest.approx(19_000.0)


# ── Realtime curtailment ──────────────────────────────────────────────────────


def test_no_limit_leaves_setpoints_alone() -> None:
    loop = _loop([_storage("bat")], [_charge("bat", 1.2)], None)
    assert _sp(loop, 9000.0, {"bat": 0.0})["bat"] == 1200.0


def test_grid_charge_is_curtailed_to_the_limit_and_released_with_hysteresis() -> None:
    limit = GridLimit(max_import_w=5000.0, hysteresis_w=250.0)
    loop = _loop([_storage("bat")], [_charge("bat", 1.2)], limit)

    # 4.5 kW house load + 1.2 kW planned → 5.7 kW: cut to 500 W.
    assert _sp(loop, 4500.0, {"bat": 0.0})["bat"] == pytest.approx(500.0)
    # Same load, battery now at 500 W: stays curtailed.
    assert _sp(loop, 4500.0, {"bat": 500.0})["bat"] == pytest.approx(500.0)
    # Load drops to 3.6 kW: full plan would be 4.8 kW, inside the band → hold.
    assert _sp(loop, 3600.0, {"bat": 500.0})["bat"] == pytest.approx(500.0)
    # Load drops to 3.5 kW: full plan 4.7 kW ≤ 4.75 kW → released.
    assert _sp(loop, 3500.0, {"bat": 500.0})["bat"] == pytest.approx(1200.0)


def test_curtailment_never_turns_charging_into_discharge() -> None:
    limit = GridLimit(max_import_w=5000.0)
    loop = _loop([_storage("bat")], [_charge("bat", 1.2)], limit)
    assert _sp(loop, 8000.0, {"bat": 0.0})["bat"] == 0.0


def test_idle_battery_is_not_discharged_to_meet_the_limit() -> None:
    limit = GridLimit(max_import_w=5000.0)
    loop = _loop(
        [_storage("bat")],
        [ControlIntent(device_id="bat", timestep=_SLOT, power_kw=0.0)],
        limit,
    )
    assert _sp(loop, 8000.0, {"bat": 0.0})["bat"] == 0.0


@pytest.mark.parametrize(
    ("order", "expected"),
    [((), {"a": 0.0, "b": 500.0}), (("b", "a"), {"a": 500.0, "b": 0.0})],
)
def test_curtail_order(order: tuple[str, ...], expected: dict[str, float]) -> None:
    limit = GridLimit(max_import_w=5000.0, curtail_order=order)
    loop = _loop([_storage("a"), _storage("b")], [_charge("a", 1.0), _charge("b", 1.0)], limit)
    sp = _sp(loop, 4500.0, {"a": 0.0, "b": 0.0})   # 1.5 kW over
    assert sp == pytest.approx(expected)


def test_storage_is_curtailed_before_evs_by_default() -> None:
    limit = GridLimit(max_import_w=15_000.0)
    loop = _loop([_FakeEv("wb", 11_000.0), _storage("bat")], [_charge("bat", 1.2)], limit)
    # 4 kW house + 11 kW EV + 1.2 kW battery = 16.2 kW: the battery goes first.
    sp = _sp(loop, 4000.0, {"wb": 0.0, "bat": 0.0})
    assert sp["bat"] == 0.0
    assert sp["wb"] == pytest.approx(11_000.0)


def test_ev_is_reduced_then_stopped_below_its_minimum() -> None:
    limit = GridLimit(max_import_w=15_000.0)
    loop = _loop([_FakeEv("wb", 11_000.0)], [], limit)
    assert _sp(loop, 6000.0, {"wb": 0.0})["wb"] == pytest.approx(9000.0)
    loop = _loop([_FakeEv("wb", 11_000.0)], [], limit)
    assert _sp(loop, 12_000.0, {"wb": 0.0})["wb"] == 0.0   # 3 kW left < 4.14 kW minimum


def test_device_drawing_less_than_commanded_is_not_curtailed() -> None:
    """A car accepting 9.7 kW of an 11 kW offer: project from what it draws."""
    limit = GridLimit(max_import_w=15_000.0, hysteresis_w=500.0)
    loop = _loop([_FakeEv("wb", 11_000.0)], [], limit)
    _sp(loop, 3000.0, {"wb": 0.0})                      # 14 kW projected: commanded 11 kW
    assert _sp(loop, 4300.0, {"wb": 9700.0})["wb"] == pytest.approx(11_000.0)  # 14.0 kW measured


def test_real_ev_contributor_curtailment() -> None:
    ev = EvChargerContributor(EvChargingAsset(
        asset_id="car", device_id="wb", label="Car", capacity_kwh=60.0,
        max_charge_kw=11.0, min_charge_kw=4.14,
    ))
    live = LiveSituation(timestamp=_NOW, grid_power_w=0.0, dt_hours=0.0)
    assert ev.curtail_setpoint_w(11_000.0, 3000.0, live) == pytest.approx(8000.0)
    assert ev.curtail_setpoint_w(11_000.0, 8000.0, live) == 0.0
    assert ev.curtail_setpoint_w(1.0, 5000.0, live) == 1.0          # PV mode: untouched
    assert ev.expected_draw_w(1.0, live) is None
    assert ev.expected_draw_w(0.0, live) == 0.0
    assert ev.expected_draw_w(7000.0, live) == 7000.0


# ── Planner ───────────────────────────────────────────────────────────────────


async def _plan_peak_import_kw(max_import_kw: float | None) -> float:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    batteries = [
        StorageConstraints(device_id=did, capacity_kwh=10.0, max_charge_kw=3.0,
                           max_discharge_kw=3.0, min_soc_pct=10.0, max_soc_pct=95.0)
        for did in ("a", "b")
    ]
    # Two cheap hours, then expensive: both batteries want to fill at once.
    prices = [0.40, 0.05, 0.05] + [0.50] * 21
    ctx = OptimizationContext(
        device_states={b.device_id: DeviceState(device_id=b.device_id, soc_pct=10.0) for b in batteries},
        storage_constraints=batteries,
        forecasts={ForecastQuantity.PRICE: [
            ForecastPoint(timestamp=now + timedelta(hours=i), value=p) for i, p in enumerate(prices)
        ]},
        horizon=timedelta(hours=24),
        max_import_kw=max_import_kw,
    )
    plan = await MilpHigsOptimizer(step_minutes=60).optimize(ctx)
    return max(f.grid_import_kw for f in plan.flows)


async def test_planner_keeps_import_below_the_limit() -> None:
    assert await _plan_peak_import_kw(None) > 5.0          # both at 3 kW in the cheap hours
    assert await _plan_peak_import_kw(4.0) <= 4.0 + 1e-3

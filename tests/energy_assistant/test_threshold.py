"""Tests for threshold-controlled device: MILP scheduling and ControlContributor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.assets.loader import parse_threshold_assets
from energy_assistant.assets.threshold import ThresholdControlContributor
from energy_assistant.core.control import ControlIntent, LiveSituation
from energy_assistant.core.models import (
    DeviceState,
    ForecastPoint,
    ForecastQuantity,
    StorageConstraints,
    ThresholdConstraints,
)
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer


# ── Helpers ───────────────────────────────────────────────────────────────────


def _prices(start: datetime, values: list[float]) -> list[ForecastPoint]:
    return [ForecastPoint(timestamp=start + timedelta(hours=i), value=v) for i, v in enumerate(values)]


def _cooler(
    device_id: str = "cooler",
    bottom: float = 24.0,
    top: float = 28.0,
    rated_power_kw: float = 0.2,
    active_rate: float = 2.0,
    drift_rate: float = 1.0,
    min_runtime_h: float = 0.0,
    min_offtime_h: float = 0.0,
) -> ThresholdConstraints:
    return ThresholdConstraints(
        device_id=device_id,
        bottom_threshold=bottom,
        top_threshold=top,
        unit="°C",
        direction="reduces",
        rated_power_kw=rated_power_kw,
        active_rate_per_h=active_rate,
        drift_rate_per_h=drift_rate,
        min_runtime_h=min_runtime_h,
        min_offtime_h=min_offtime_h,
    )


def _dehumidifier(
    device_id: str = "dehum",
    bottom: float = 40.0,
    top: float = 65.0,
    rated_power_kw: float = 0.35,
    active_rate: float = 5.0,
    drift_rate: float = 2.0,
) -> ThresholdConstraints:
    return ThresholdConstraints(
        device_id=device_id,
        bottom_threshold=bottom,
        top_threshold=top,
        unit="%RH",
        direction="reduces",
        rated_power_kw=rated_power_kw,
        active_rate_per_h=active_rate,
        drift_rate_per_h=drift_rate,
    )


def _battery(
    device_id: str = "bat",
    capacity_kwh: float = 10.0,
    max_charge_kw: float = 3.0,
) -> StorageConstraints:
    return StorageConstraints(
        device_id=device_id,
        capacity_kwh=capacity_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_charge_kw,
        min_soc_pct=10.0,
        max_soc_pct=95.0,
    )


def _live(
    device_id: str,
    measured_value: float | None = None,
    power_w: float = 0.0,
    price: float = 0.25,
    grid_power_w: float = 0.0,
) -> LiveSituation:
    extra = {"measured_value": measured_value} if measured_value is not None else {}
    state = DeviceState(device_id=device_id, power_w=power_w, extra=extra)
    return LiveSituation(
        timestamp=datetime.now(timezone.utc),
        grid_power_w=grid_power_w,
        dt_hours=30 / 3600,
        device_states={device_id: state},
        current_price_eur_per_kwh=price,
    )


def _intent(device_id: str, mode: str) -> ControlIntent:
    return ControlIntent(
        device_id=device_id,
        timestep=datetime.now(timezone.utc),
        power_kw=1.0 if mode == "run" else 0.0,
    )


# ── MILP optimizer tests ──────────────────────────────────────────────────────


class TestThresholdMilp:
    """Tests for threshold device integration in the MILP optimizer."""

    async def test_returns_run_and_standby_intents(self) -> None:
        """Optimizer produces both 'run' and 'standby' intents for a threshold device."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        tc = _cooler()
        ctx = OptimizationContext(
            device_states={
                "bat": DeviceState(device_id="bat", soc_pct=50.0),
                "cooler": DeviceState(
                    device_id="cooler",
                    extra={"measured_value": 26.0},  # mid-range — optimizer has flexibility
                ),
            },
            storage_constraints=[_battery()],
            threshold_constraints=[tc],
            forecasts={ForecastQuantity.PRICE: _prices(now, [0.25] * 24)},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        threshold_intents = [i for i in plan.intents if i.device_id == "cooler"]
        assert len(threshold_intents) == 24
        assert all(i.power_kw >= 0 for i in threshold_intents)
        assert any(i.power_kw > 0 for i in threshold_intents)
        assert any(i.power_kw == 0 for i in threshold_intents)

    async def test_near_top_threshold_forces_more_running(self) -> None:
        """Value near top threshold causes optimizer to schedule more runtime than mid-range."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        tc = _cooler(active_rate=2.0, drift_rate=1.0)

        def _ctx(measured: float) -> OptimizationContext:
            return OptimizationContext(
                device_states={
                    "bat": DeviceState(device_id="bat", soc_pct=50.0),
                    "cooler": DeviceState(device_id="cooler", extra={"measured_value": measured}),
                },
                storage_constraints=[_battery()],
                threshold_constraints=[tc],
                forecasts={ForecastQuantity.PRICE: _prices(now, [0.25] * 24)},
                horizon=timedelta(hours=24),
            )

        plan_mid = await optimizer.optimize(_ctx(26.0))   # mid-range
        plan_hot = await optimizer.optimize(_ctx(27.5))   # near top (28)

        run_mid = sum(1 for i in plan_mid.intents if i.device_id == "cooler" and i.power_kw > 0)
        run_hot = sum(1 for i in plan_hot.intents if i.device_id == "cooler" and i.power_kw > 0)

        assert run_hot >= run_mid, (
            f"Near-top should need ≥ as many run slots as mid-range ({run_hot} vs {run_mid})"
        )

    async def test_cheap_hours_preferred_for_runtime(self) -> None:
        """Optimizer schedules runtime in cheap hours when value is in the safe zone."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # 24 hours: hours 0-11 expensive (0.40), hours 12-23 cheap (0.10)
        prices = [0.40] * 12 + [0.10] * 12
        # Active rate 4 /h, drift 1 /h — needs to run ≈ 1/5 of the time to stay level.
        # Starting at midpoint 26 gives margin to shift all runtime to cheap hours.
        tc = _cooler(active_rate=4.0, drift_rate=1.0, bottom=22.0, top=30.0)
        ctx = OptimizationContext(
            device_states={
                "bat": DeviceState(device_id="bat", soc_pct=50.0),
                "cooler": DeviceState(device_id="cooler", extra={"measured_value": 26.0}),
            },
            storage_constraints=[_battery()],
            threshold_constraints=[tc],
            forecasts={ForecastQuantity.PRICE: _prices(now, prices)},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        cooler_intents = {i.timestep: i for i in plan.intents if i.device_id == "cooler"}
        expensive_runs = sum(
            1 for t, i in cooler_intents.items()
            if i.power_kw > 0 and t < now + timedelta(hours=12)
        )
        cheap_runs = sum(
            1 for t, i in cooler_intents.items()
            if i.power_kw > 0 and t >= now + timedelta(hours=12)
        )
        assert cheap_runs >= expensive_runs, (
            f"Optimizer should prefer cheap hours: cheap={cheap_runs}, expensive={expensive_runs}"
        )

    async def test_min_runtime_and_offtime_respected_in_plan(self) -> None:
        """With 15-min steps and 0.5 h min run/off time, every planned run and
        off block spans at least two steps — no single-slot blips in the plan."""
        optimizer = MilpHigsOptimizer(step_minutes=15)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Aquarium-cooler-like physics: ~31% duty cycle, must cycle repeatedly.
        tc = _cooler(
            bottom=18.0, top=20.5,
            active_rate=0.417, drift_rate=0.185,
            rated_power_kw=0.15,
            min_runtime_h=0.5, min_offtime_h=0.5,
        )
        # Alternating cheap/expensive 15-min prices tempt the solver into
        # single-step runs on the cheap slots — the constraints must forbid it.
        prices = [
            ForecastPoint(
                timestamp=now + timedelta(minutes=15 * i),
                value=0.10 if i % 2 == 0 else 0.40,
            )
            for i in range(96)
        ]
        ctx = OptimizationContext(
            device_states={
                "bat": DeviceState(device_id="bat", soc_pct=50.0),
                "cooler": DeviceState(device_id="cooler", extra={"measured_value": 19.25}),
            },
            storage_constraints=[_battery()],
            threshold_constraints=[tc],
            forecasts={ForecastQuantity.PRICE: prices},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        states = [
            i.power_kw > 0
            for i in sorted(
                (i for i in plan.intents if i.device_id == "cooler"),
                key=lambda i: i.timestep,
            )
        ]
        assert len(states) == 96
        assert any(states), "cooler must run at some point over 24 h"

        # Collect (is_running, length) blocks of consecutive equal states.
        blocks: list[tuple[bool, int]] = []
        for s in states:
            if blocks and blocks[-1][0] == s:
                blocks[-1] = (s, blocks[-1][1] + 1)
            else:
                blocks.append((s, 1))

        # Run blocks: all ≥ 2 steps (last may be truncated by horizon end).
        # Off blocks: interior ones ≥ 2 steps (the first isn't a planned stop,
        # the last may be truncated by the horizon).
        for idx, (running, length) in enumerate(blocks):
            truncated = idx == len(blocks) - 1
            if running and not truncated:
                assert length >= 2, f"run block of {length} step(s) violates min_runtime"
            if not running and idx > 0 and not truncated:
                assert length >= 2, f"off block of {length} step(s) violates min_offtime"

    async def test_out_of_bounds_measured_value_does_not_collapse_plan(self) -> None:
        """A live value past the top threshold is clamped for planning instead
        of making the MILP infeasible (which would return an empty plan)."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        tc = _cooler(bottom=24.0, top=28.0)
        ctx = OptimizationContext(
            device_states={
                "bat": DeviceState(device_id="bat", soc_pct=50.0),
                "cooler": DeviceState(device_id="cooler", extra={"measured_value": 29.3}),
            },
            storage_constraints=[_battery()],
            threshold_constraints=[tc],
            forecasts={ForecastQuantity.PRICE: _prices(now, [0.25] * 24)},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        cooler_intents = sorted(
            (i for i in plan.intents if i.device_id == "cooler"),
            key=lambda i: i.timestep,
        )
        assert len(cooler_intents) == 24, "plan must not collapse to empty"
        # Clamped to the top threshold, the only feasible first step is to run.
        assert cooler_intents[0].power_kw > 0

    async def test_no_threshold_devices_still_works(self) -> None:
        """Plan succeeds when no threshold constraints are passed (backward compat)."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        ctx = OptimizationContext(
            device_states={"bat": DeviceState(device_id="bat", soc_pct=50.0)},
            storage_constraints=[_battery()],
            forecasts={ForecastQuantity.PRICE: _prices(now, [0.25] * 24)},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        assert plan is not None
        assert not any(i.device_id == "cooler" for i in plan.intents)

    async def test_two_threshold_devices_independent(self) -> None:
        """Cooler and dehumidifier each get their own intents with correct device_id."""
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        ctx = OptimizationContext(
            device_states={
                "bat": DeviceState(device_id="bat", soc_pct=50.0),
                "cooler": DeviceState(device_id="cooler", extra={"measured_value": 26.0}),
                "dehum": DeviceState(device_id="dehum", extra={"measured_value": 52.0}),
            },
            storage_constraints=[_battery()],
            threshold_constraints=[_cooler(), _dehumidifier()],
            forecasts={ForecastQuantity.PRICE: _prices(now, [0.25] * 24)},
            horizon=timedelta(hours=24),
        )
        plan = await optimizer.optimize(ctx)
        cooler_intents = [i for i in plan.intents if i.device_id == "cooler"]
        dehum_intents = [i for i in plan.intents if i.device_id == "dehum"]
        assert len(cooler_intents) == 24
        assert len(dehum_intents) == 24


# ── Config parsing tests ─────────────────────────────────────────────────────


class TestParseThresholdAssets:
    """Tests for rated power resolution between asset and device config."""

    _BASE_ASSET = {
        "type": "threshold",
        "device": "cooler",
        "bottom_threshold": 18.0,
        "top_threshold": 20.5,
        "active_rate_per_h": 0.417,
        "drift_rate_per_h": 0.185,
    }

    def test_rated_power_falls_back_to_device(self) -> None:
        devices = {"cooler": {"type": "threshold_homeassistant", "rated_power_w": 150}}
        parsed = parse_threshold_assets({"aquarium": dict(self._BASE_ASSET)}, devices)
        assert len(parsed) == 1
        assert parsed[0].rated_power_kw == pytest.approx(0.150)

    def test_asset_rated_power_overrides_device(self) -> None:
        asset = dict(self._BASE_ASSET, rated_power_kw=0.2)
        devices = {"cooler": {"rated_power_w": 150}}
        parsed = parse_threshold_assets({"aquarium": asset}, devices)
        assert parsed[0].rated_power_kw == pytest.approx(0.2)

    def test_missing_rated_power_everywhere_skips_asset(self) -> None:
        parsed = parse_threshold_assets({"aquarium": dict(self._BASE_ASSET)}, {})
        assert parsed == []


# ── ThresholdControlContributor tests ────────────────────────────────────────


class TestThresholdControlContributor:
    """Unit tests for the control contributor."""

    def test_run_intent_returns_rated_power(self) -> None:
        tc = _cooler(rated_power_kw=0.2)
        contrib = ThresholdControlContributor(tc)
        live = _live("cooler", measured_value=26.0)
        result = contrib.desired_setpoint_w(_intent("cooler", "run"), live)
        assert result == pytest.approx(200.0)

    def test_standby_intent_returns_zero(self) -> None:
        tc = _cooler(rated_power_kw=0.2)
        contrib = ThresholdControlContributor(tc)
        live = _live("cooler", measured_value=26.0)
        result = contrib.desired_setpoint_w(_intent("cooler", "standby"), live)
        assert result == 0.0

    def test_no_intent_defaults_to_standby(self) -> None:
        tc = _cooler()
        contrib = ThresholdControlContributor(tc)
        live = _live("cooler", measured_value=26.0)
        result = contrib.desired_setpoint_w(None, live)
        assert result == 0.0

    def test_emergency_on_at_top_threshold(self) -> None:
        """Value at top threshold forces device ON, overriding standby intent."""
        tc = _cooler(top=28.0, rated_power_kw=0.2)
        contrib = ThresholdControlContributor(tc)
        live = _live("cooler", measured_value=28.0)
        result = contrib.desired_setpoint_w(_intent("cooler", "standby"), live)
        assert result == pytest.approx(200.0)

    def test_emergency_off_at_bottom_threshold(self) -> None:
        """Value at bottom threshold forces device OFF, overriding run intent."""
        tc = _cooler(bottom=24.0)
        contrib = ThresholdControlContributor(tc)
        live = _live("cooler", measured_value=24.0)
        result = contrib.desired_setpoint_w(_intent("cooler", "run"), live)
        assert result == 0.0

    def test_increases_direction_emergency_on_at_bottom(self) -> None:
        """For 'increases' direction (heater): force ON at bottom threshold."""
        tc = ThresholdConstraints(
            device_id="heater",
            bottom_threshold=18.0,
            top_threshold=22.0,
            direction="increases",
            rated_power_kw=1.5,
            active_rate_per_h=3.0,
            drift_rate_per_h=1.0,
        )
        contrib = ThresholdControlContributor(tc)
        live = _live("heater", measured_value=18.0)
        result = contrib.desired_setpoint_w(_intent("heater", "standby"), live)
        assert result == pytest.approx(1500.0)

    def test_min_runtime_keeps_device_on(self) -> None:
        """Device stays on if min_runtime_h not yet elapsed, even on standby intent."""
        tc = ThresholdConstraints(
            device_id="cooler",
            bottom_threshold=24.0,
            top_threshold=28.0,
            direction="reduces",
            rated_power_kw=0.2,
            active_rate_per_h=2.0,
            drift_rate_per_h=1.0,
            min_runtime_h=0.25,  # 15 minutes
        )
        contrib = ThresholdControlContributor(tc)
        now = datetime.now(timezone.utc)
        # Simulate: device just started 5 min ago
        contrib._running_since = now - timedelta(minutes=5)
        contrib._stopped_since = None
        # Device is currently running (200 W) and optimizer says standby
        live = _live("cooler", measured_value=26.5, power_w=200.0)
        live = LiveSituation(
            timestamp=now,
            grid_power_w=0.0,
            dt_hours=30 / 3600,
            device_states={"cooler": DeviceState(device_id="cooler", power_w=200.0, extra={"measured_value": 26.5})},
            current_price_eur_per_kwh=0.25,
        )
        result = contrib.desired_setpoint_w(_intent("cooler", "standby"), live)
        assert result == pytest.approx(200.0), "Should keep running — min_runtime not elapsed"

    def test_min_offtime_keeps_device_off(self) -> None:
        """Device stays off if min_offtime_h not yet elapsed, even on run intent."""
        tc = ThresholdConstraints(
            device_id="cooler",
            bottom_threshold=24.0,
            top_threshold=28.0,
            direction="reduces",
            rated_power_kw=0.2,
            active_rate_per_h=2.0,
            drift_rate_per_h=1.0,
            min_offtime_h=0.083,  # 5 minutes
        )
        contrib = ThresholdControlContributor(tc)
        now = datetime.now(timezone.utc)
        # Simulate: device stopped 2 min ago
        contrib._stopped_since = now - timedelta(minutes=2)
        contrib._running_since = None
        live = LiveSituation(
            timestamp=now,
            grid_power_w=0.0,
            dt_hours=30 / 3600,
            device_states={"cooler": DeviceState(device_id="cooler", power_w=0.0, extra={"measured_value": 27.5})},
            current_price_eur_per_kwh=0.25,
        )
        result = contrib.desired_setpoint_w(_intent("cooler", "run"), live)
        assert result == 0.0, "Should stay off — min_offtime not elapsed"


class TestThresholdHADeviceReconciliation:
    """send_command must check the actual switch state before calling HA."""

    @staticmethod
    def _device(client):
        from energy_assistant.plugins.threshold_homeassistant.device import ThresholdHADevice
        return ThresholdHADevice(
            "cooler", client,
            entity_sensor="sensor.temp", entity_switch="switch.cooler",
            rated_power_w=150.0,
        )

    async def test_skips_service_call_when_already_in_desired_state(self) -> None:
        from tests.helpers.fake_ha_client import FakeHAClient
        from energy_assistant.core.models import DeviceCommand
        client = FakeHAClient(states={"switch.cooler": "on"})
        await self._device(client).send_command(
            DeviceCommand(device_id="cooler", command="set_power_w", value=150.0)
        )
        assert client.calls == []

    async def test_corrects_mismatched_switch(self) -> None:
        """Should be on but is off (e.g. manually toggled) → turn_on sent."""
        from tests.helpers.fake_ha_client import FakeHAClient
        from energy_assistant.core.models import DeviceCommand
        client = FakeHAClient(states={"switch.cooler": "off"})
        await self._device(client).send_command(
            DeviceCommand(device_id="cooler", command="set_power_w", value=150.0)
        )
        assert client.calls == [
            ("homeassistant", "turn_on", {"entity_id": "switch.cooler"}),
        ]

    async def test_turns_off_when_running_but_standby_desired(self) -> None:
        from tests.helpers.fake_ha_client import FakeHAClient
        from energy_assistant.core.models import DeviceCommand
        client = FakeHAClient(states={"switch.cooler": "on"})
        await self._device(client).send_command(
            DeviceCommand(device_id="cooler", command="set_power_w", value=0.0)
        )
        assert client.calls == [
            ("homeassistant", "turn_off", {"entity_id": "switch.cooler"}),
        ]

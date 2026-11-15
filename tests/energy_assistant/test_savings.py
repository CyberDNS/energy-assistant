"""Tests for the savings accounting (core/savings.py) and its persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from energy_assistant.core.models import (
    DeviceState,
    EnergyPlan,
    ForecastPoint,
    ForecastQuantity,
    PlanFlow,
    StorageConstraints,
)
from energy_assistant.core.optimizer import OptimizationContext, SecondaryTariffLoad
from energy_assistant.core.savings import (
    ActualSlot,
    EvLive,
    EvStep,
    LiveSample,
    PredictedSlot,
    SavingsTracker,
    ShadowState,
    baseline_step,
    day_ahead_window_end,
    predict_slots,
    set_ev_need,
    settle,
)
from energy_assistant.plugins.flat_rate.tariff import FlatRateTariff
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer
from energy_assistant.plugins.mqtt_publisher import _savings_discovery_configs
from energy_assistant.storage.sqlite import SqliteStorageBackend

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
BAT = StorageConstraints(
    device_id="bat", capacity_kwh=10.0, max_charge_kw=4.0, max_discharge_kw=4.0,
    charge_efficiency=1.0, discharge_efficiency=1.0,
)


def _sample(
    minutes: float,
    site_kw: float,
    *,
    storage_kw: float = 0.0,
    stored: float = 5.0,
    basis: float = 0.20,
    price: float = 0.30,
    ev: EvLive | None = None,
    secondary: list[tuple[float, float]] | None = None,
) -> LiveSample:
    return LiveSample(
        timestamp=T0 + timedelta(minutes=minutes),
        site_net_kw=site_kw,
        storage_kw={"bat": storage_kw},
        stored_kwh={"bat": stored},
        cost_basis={"bat": basis},
        ev={"cp": ev} if ev else {},
        secondary=secondary or [],
        import_price=price,
        export_price=0.08,
    )


class TestSettle:
    def test_import_and_export(self) -> None:
        assert settle(2.0, [], 0.30, 0.08) == pytest.approx(0.60)
        assert settle(-2.0, [], 0.30, 0.08) == pytest.approx(-0.16)

    def test_surplus_feeds_separately_billed_load_before_export(self) -> None:
        # 3 kWh surplus, heat pump needs 1 kWh at 0.18 → 2 kWh exported.
        assert settle(-3.0, [(1.0, 0.18)], 0.30, 0.08) == pytest.approx(-0.16)
        # No surplus: the heat pump buys at its own price.
        assert settle(1.0, [(1.0, 0.18)], 0.30, 0.08) == pytest.approx(0.48)


class TestBaselineStep:
    def test_battery_covers_site_deficit_including_secondary_load(self) -> None:
        state = ShadowState(stored_kwh={"bat": 5.0})
        cost = baseline_step(
            state, [BAT], residual_kwh=2.0, secondary=[(1.0, 0.18)], ev={},
            import_price=0.30, export_price=0.08, hours=1.0,
        )
        assert state.stored_kwh["bat"] == pytest.approx(3.0)
        assert cost == pytest.approx(0.0)

    def test_battery_absorbs_surplus_up_to_power_limit(self) -> None:
        state = ShadowState(stored_kwh={"bat": 5.0})
        cost = baseline_step(
            state, [BAT], residual_kwh=-6.0, secondary=[], ev={},
            import_price=0.30, export_price=0.08, hours=1.0,
        )
        assert state.stored_kwh["bat"] == pytest.approx(9.0)
        assert cost == pytest.approx(-2.0 * 0.08)

    def test_unplanned_car_charges_at_full_power_until_need_is_met(self) -> None:
        state = ShadowState(stored_kwh={"bat": 0.0})
        set_ev_need(state, "cp", 15.0)
        step = {"cp": EvStep(plugged=True, real_kwh=0.0, max_kw=11.0)}
        kwargs = dict(residual_kwh=0.0, secondary=[], ev=step,
                      import_price=0.30, export_price=0.08, hours=1.0)
        assert baseline_step(state, [BAT], **kwargs) == pytest.approx(11.0 * 0.30)
        assert baseline_step(state, [BAT], **kwargs) == pytest.approx(4.0 * 0.30)
        assert baseline_step(state, [BAT], **kwargs) == pytest.approx(0.0)


class TestSavingsTracker:
    def _run(self, tracker: SavingsTracker, samples: list[LiveSample]) -> list[ActualSlot]:
        slots: list[ActualSlot] = []
        for s in samples:
            slots += tracker.tick(s)
        return slots

    def test_same_behaviour_as_unplanned_saves_nothing(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=15)
        # House draws 1 kW; the real battery covers it, exactly like unplanned.
        samples = [
            _sample(m, 0.0, storage_kw=-1.0, stored=5.0 - m / 60.0) for m in range(0, 32)
        ]
        slots = self._run(tracker, samples)
        assert len(slots) == 2
        assert slots[0].hours == pytest.approx(0.25)
        assert slots[1].hours == pytest.approx(0.25)
        for slot in slots:
            assert slot.saving == pytest.approx(0.0, abs=1e-9)

    def test_holding_charge_for_expensive_hour_shows_the_spread(self) -> None:
        """Real battery idles at 0.20 and discharges at 0.40; unplanned does
        the opposite.  Over both hours the saving is 1 kWh × the spread."""
        tracker = SavingsTracker([BAT.model_copy(update={"min_soc_pct": 40.0})], step_minutes=60)
        cheap = [_sample(m, 1.0, stored=5.0, price=0.20) for m in range(0, 61)]
        dear = [
            _sample(60 + m, 0.0, storage_kw=-1.0, stored=5.0 - m / 60.0, price=0.40)
            for m in range(1, 62)
        ]
        slots = self._run(tracker, cheap + dear)
        assert len(slots) == 2
        # Hour 1: paid 0.20 while unplanned paid nothing, but 1 kWh more is
        # still stored (valued at the 0.20 basis) → no loss booked yet.
        assert slots[0].saving == pytest.approx(0.0, abs=1e-6)
        # Hour 2: unplanned battery is at its floor and imports at 0.40.
        assert slots[1].saving == pytest.approx(0.20, abs=1e-3)

    def test_gap_in_measurements_is_not_booked(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=15)
        samples = [_sample(0, 1.0), _sample(1, 1.0), _sample(10, 1.0, stored=2.0), _sample(16, 1.0)]
        slots = self._run(tracker, samples)
        assert len(slots) == 1
        assert slots[0].hours == pytest.approx(1 / 60)
        # Only the observed minute counts (the unplanned battery discharged
        # 1/60 kWh, the real one did not); the 3 kWh that vanished unobserved
        # are neither a loss nor a saving.
        assert slots[0].inventory_delta == pytest.approx(1 / 60 * 0.20)

    def test_deferred_ev_charge_is_valued_until_it_catches_up(self) -> None:
        tracker = SavingsTracker([], step_minutes=60)
        tracker.tick(_sample(0, 0.0, ev=EvLive(True, 0.0, 6.0)))
        set_ev_need(tracker.state, "cp", 6.0)
        # Hour 1 (0.40): unplanned charges 6 kWh, real car waits.
        wait = [_sample(m, 0.0, price=0.40, ev=EvLive(True, 0.0, 6.0)) for m in range(1, 61)]
        # Hour 2 (0.25): real car charges the 6 kWh.
        charge = [_sample(60 + m, 6.0, price=0.25, ev=EvLive(True, 6.0, 6.0)) for m in range(1, 62)]
        slots = self._run(tracker, wait + charge)
        assert slots[0].baseline_cost == pytest.approx(2.40)
        assert slots[0].saving == pytest.approx(0.0, abs=1e-6)
        assert slots[1].saving == pytest.approx(6.0 * 0.15, abs=1e-2)
        # Unplugging closes the session without moving the inventory value.
        unplug = [_sample(122 + m, 0.0, ev=EvLive(False, 0.0, 6.0)) for m in range(0, 60)]
        (third,) = self._run(tracker, unplug)[-1:]
        assert third.saving == pytest.approx(0.0, abs=1e-2)
        assert tracker.state.evs == {}

    def test_state_round_trips_through_json(self) -> None:
        state = ShadowState(stored_kwh={"bat": 3.0}, closed_ev_value=-0.5)
        set_ev_need(state, "cp", 4.0)
        assert ShadowState.from_dict(state.to_dict()) == state


class TestPredictSlots:
    def _flow(self, i: int, **kw) -> PlanFlow:
        base = dict(
            timestep=T0 + timedelta(hours=i), pv_kw=0.0, grid_import_kw=0.0,
            grid_export_kw=0.0, hours=1.0, load_kw=1.0, import_price=0.30,
            export_price=0.08, stored_kwh={"bat": 5.0},
        )
        return PlanFlow(**{**base, **kw})

    def test_plan_without_economics_predicts_nothing(self) -> None:
        plan = EnergyPlan(flows=[PlanFlow(timestep=T0, pv_kw=0, grid_import_kw=0, grid_export_kw=0)])
        assert predict_slots(plan, ShadowState(), [BAT], stored_kwh={}, cost_basis={}, ev_max_kw={}) == []

    def test_matches_tracker_arithmetic_and_leaves_state_untouched(self) -> None:
        bat = BAT.model_copy(update={"min_soc_pct": 40.0})
        plan = EnergyPlan(step_minutes=60, flows=[
            self._flow(0, import_price=0.20),
            self._flow(1, import_price=0.40, storage_kw=-1.0, stored_kwh={"bat": 4.0}),
        ])
        state = ShadowState(stored_kwh={"bat": 5.0})
        slots = predict_slots(
            plan, state, [bat], stored_kwh={"bat": 5.0}, cost_basis={"bat": 0.20}, ev_max_kw={},
        )
        assert [round(s.saving, 6) for s in slots] == [0.0, 0.20]
        assert state.stored_kwh == {"bat": 5.0}

    async def test_optimizer_plan_predicts_a_non_negative_saving(self) -> None:
        """End to end: a solved plan is at least as cheap as the unplanned
        site once the energy left in the battery is taken into account."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = 12
        prices = [0.20] * 4 + [0.45] * 4 + [0.25] * 4
        ctx = OptimizationContext(
            device_states={"bat": DeviceState(device_id="bat", soc_pct=50.0)},
            storage_constraints=[BAT.model_copy(update={"charge_efficiency": 0.95,
                                                        "discharge_efficiency": 0.95})],
            tariffs={"grid": FlatRateTariff("grid", export_price_eur_per_kwh=0.08)},
            forecasts={
                ForecastQuantity.PRICE: [
                    ForecastPoint(timestamp=now + timedelta(hours=h), value=p)
                    for h, p in enumerate(prices)
                ],
                ForecastQuantity.CONSUMPTION: [
                    ForecastPoint(timestamp=now + timedelta(hours=h), value=1.5)
                    for h in range(hours)
                ],
            },
            secondary_loads=[SecondaryTariffLoad(
                tariff_id="hp",
                load=[ForecastPoint(timestamp=now, value=0.5)],
                prices=[ForecastPoint(timestamp=now, value=0.18)],
            )],
            horizon=timedelta(hours=hours),
            battery_cost_basis={"bat": 0.20},
        )
        plan = await MilpHigsOptimizer(step_minutes=60).optimize(ctx)
        flow = plan.flows[1]
        assert flow.hours == 1.0 and flow.load_kw == pytest.approx(1.0)
        assert flow.secondary_kw == [0.5] and flow.secondary_prices == [0.18]
        assert set(flow.stored_kwh) == {"bat"}

        slots = predict_slots(
            plan, ShadowState(), ctx.storage_constraints,
            stored_kwh={"bat": 5.0}, cost_basis={"bat": 0.20}, ev_max_kw={},
        )
        assert len(slots) == len(plan.flows)
        assert sum(s.saving for s in slots) > 0.05


class TestDayAheadWindow:
    def test_day_opens_shortly_before_local_midnight(self) -> None:
        lead = timedelta(minutes=30)
        tomorrow = datetime.now().date() + timedelta(days=1)
        local_midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day).astimezone()
        next_midnight = datetime.fromordinal(tomorrow.toordinal() + 1).astimezone()
        # Well before midnight: only the rest of today.
        assert day_ahead_window_end(local_midnight - timedelta(hours=5), lead) == local_midnight
        # Inside the lead: all of tomorrow.
        assert day_ahead_window_end(local_midnight - timedelta(minutes=10), lead) == next_midnight


class TestSavingsStorage:
    async def test_only_slots_with_day_ahead_prediction_are_compared(self, tmp_path: Path) -> None:
        db = SqliteStorageBackend(tmp_path / "h.db")
        await db.start()
        try:
            s0, s1, s2 = (T0 + timedelta(minutes=15 * i) for i in range(3))
            pred = lambda start, base: PredictedSlot(start, 0.25, base, 0.10, 0.0)  # noqa: E731
            await db.save_savings_predictions("day_ahead", [pred(s0, 0.30), pred(s1, 0.50)], T0, overwrite=False)
            # A later plan must not move a frozen prediction …
            await db.save_savings_predictions("day_ahead", [pred(s0, 9.0)], T0, overwrite=False)
            # … but always refreshes the latest one.
            await db.save_savings_predictions("latest", [pred(s0, 0.20)], T0, overwrite=True)
            await db.save_savings_predictions("latest", [pred(s0, 0.25)], T0, overwrite=True)

            await db.save_savings_actual(ActualSlot(s0, 0.25, 0.40, 0.15, 0.01))
            await db.save_savings_actual(ActualSlot(s1, 0.125, 0.20, 0.10, 0.0))  # half covered
            await db.save_savings_actual(ActualSlot(s2, 0.25, 1.0, 0.0, 0.0))     # no prediction

            rows = await db.query_savings(T0, T0 + timedelta(hours=1))
            assert [r["slot_start"] for r in rows] == [s0, s1]
            assert rows[0]["real"] == pytest.approx(0.26)
            assert rows[0]["predicted"] == pytest.approx(0.20)
            assert rows[0]["predicted_latest"] == pytest.approx(0.15)
            # Prediction scaled to the measured half of the slot.
            assert rows[1]["predicted"] == pytest.approx(0.20)
            assert rows[1]["predicted_latest"] is None
        finally:
            await db.stop()

    async def test_state_round_trip(self, tmp_path: Path) -> None:
        db = SqliteStorageBackend(tmp_path / "h.db")
        await db.start()
        try:
            assert await db.load_savings_state() is None
            await db.save_savings_state({"stored_kwh": {"bat": 2.5}})
            saved_at, state = await db.load_savings_state()
            assert state == {"stored_kwh": {"bat": 2.5}}
            assert datetime.now(timezone.utc) - saved_at < timedelta(minutes=1)
        finally:
            await db.stop()


class TestServerWiring:
    """The Application glue: live sample → slot rows, plan → predictions."""

    async def _app(self, tmp_path: Path):
        from types import SimpleNamespace

        from energy_assistant.core.change_gate import ChangeGate
        from energy_assistant.core.config import AppConfig
        from energy_assistant.core.ledger import BatteryCostLedger
        from energy_assistant.core.models import DeviceRole
        from energy_assistant.server import Application

        app = Application.__new__(Application)
        app._cfg = AppConfig(
            devices={"hp": {"tariff": "heatpump"}, "grid": {}, "bat": {}},
            default_tariff_id="household",
        )
        app._tariffs = {"household": object(), "heatpump": object()}
        app._registry = SimpleNamespace(
            get=lambda did: SimpleNamespace(
                role=DeviceRole.CONSUMER if did == "hp" else DeviceRole.METER
            )
        )
        app._grid_meter_id = "grid"
        app._poll_interval_s = 5.0
        app._plan_interval_s = 900.0
        app._storage_constraints = [BAT]
        app._ev_assets = []
        app._ledger = BatteryCostLedger()
        app._ledger.initialise("bat", 5.0, 0.20)
        app._pv_opportunity_price = 0.08
        app._mqtt_publisher = None
        app._dry_run_log_gate = ChangeGate()
        app._tariff_price_cache = (float("inf"), {"household": 0.30, "heatpump": 0.18})
        app._savings = SavingsTracker([BAT], step_minutes=15)
        app._storage = SqliteStorageBackend(tmp_path / "h.db")
        await app._storage.start()
        return app

    async def test_ticks_become_slot_rows_and_missing_readings_are_skipped(
        self, tmp_path: Path
    ) -> None:
        app = await self._app(tmp_path)
        try:
            def states(ts: datetime, **missing) -> dict:
                out = {
                    "grid": DeviceState(device_id="grid", timestamp=ts, power_w=1500.0),
                    "bat": DeviceState(device_id="bat", timestamp=ts, power_w=0.0),
                    "hp": DeviceState(device_id="hp", timestamp=ts, power_w=500.0),
                }
                out.update(missing)
                return out

            # No battery reading → no sample at all.
            assert await app._savings_sample(
                T0, 0.30, states(T0, bat=DeviceState(device_id="bat", timestamp=T0))
            ) is None
            sample = await app._savings_sample(T0, 0.30, states(T0))
            assert sample.secondary == [(0.5, 0.18)]

            for m in range(0, 17):
                ts = T0 + timedelta(minutes=m)
                await app._track_savings(ts, 0.30, states(ts))

            await app._storage.save_savings_predictions(
                "day_ahead", [PredictedSlot(T0, 0.25, 0.0, 0.0, 0.0)], T0, overwrite=False
            )
            (row,) = await app._storage.query_savings(T0, T0 + timedelta(hours=1))
            # Real: 1 kW household at 0.30 + 0.5 kW heat pump at 0.18, for 15 min.
            assert row["actual_cost"] == pytest.approx(0.25 * (0.30 + 0.09))
            # Unplanned: the battery covers all 1.5 kW.
            assert row["baseline_cost"] == pytest.approx(0.0)
            # The 0.375 kWh the real battery kept are worth their 0.20 basis.
            assert row["real"] == pytest.approx(0.375 * 0.20 - 0.25 * 0.39)
            assert (await app._storage.load_savings_state())[1]["stored_kwh"]["bat"] < 5.0
        finally:
            await app._storage.stop()

    async def test_plan_freezes_only_whole_future_slots_of_the_day(self, tmp_path: Path) -> None:
        app = await self._app(tmp_path)
        try:
            now = datetime.now(timezone.utc)
            first = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 15)
            plan = EnergyPlan(step_minutes=15, flows=[
                PlanFlow(
                    timestep=first + timedelta(minutes=15 * i), pv_kw=0.0,
                    grid_import_kw=1.0, grid_export_kw=0.0,
                    hours=0.1 if i == 0 else 0.25, load_kw=1.0,
                    import_price=0.30, export_price=0.08, stored_kwh={"bat": 5.0},
                )
                for i in range(96 * 3)
            ])
            await app._record_savings_predictions(plan)
            db = app._storage._db
            async with db.execute(
                "SELECT kind, COUNT(*), MIN(slot_start), MAX(slot_start) "
                "FROM savings_predicted GROUP BY kind ORDER BY kind"
            ) as cur:
                (_, n_day, day_min, day_max), (_, n_latest, _, _) = await cur.fetchall()
            assert n_latest == 96 * 3 - 1                       # running slot excluded
            assert datetime.fromisoformat(day_min) == first + timedelta(minutes=15)
            window_end = day_ahead_window_end(now, timedelta(minutes=30))
            assert datetime.fromisoformat(day_max) == window_end - timedelta(minutes=15)
            assert n_day <= 96 * 2
        finally:
            await app._storage.stop()


def test_kpi_sensors_are_monetary_totals() -> None:
    configs = {cfg["unique_id"]: cfg for _, cfg in _savings_discovery_configs()}
    assert set(configs) == {
        "ea_savings_real", "ea_savings_predicted",
        "ea_savings_deviation", "ea_savings_predicted_last_plan",
    }
    for cfg in configs.values():
        assert cfg["device_class"] == "monetary"
        assert cfg["state_class"] == "total"
    assert configs["ea_savings_deviation"]["value_template"] == "{{ value_json.deviation }}"

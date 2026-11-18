"""Tests for the savings accounting (core/savings.py) and its persistence.

Real house vs. a simulated uncontrolled house, ledger on both sides (#82).
Batteries here are lossless (η = 1) so the numbers can be checked by hand.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from energy_assistant.core.models import DeviceState, StorageConstraints
from energy_assistant.core.savings import (
    EvLive,
    Ledger,
    LiveSample,
    SavingsSlot,
    SavingsState,
    SavingsTracker,
    day_ahead_window_end,
    settle,
)
from energy_assistant.plugins.mqtt_publisher import _savings_discovery_configs
from energy_assistant.storage.sqlite import SqliteStorageBackend

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
BAT = StorageConstraints(
    device_id="bat", capacity_kwh=10.0, max_charge_kw=4.0, max_discharge_kw=4.0,
    charge_efficiency=1.0, discharge_efficiency=1.0, min_soc_pct=10.0,
)
MIN = 1.0  # BAT's minimum stored energy (kWh)


def _sample(
    minutes: float,
    site_kw: float,
    *,
    storage_kw: float = 0.0,
    stored: float = MIN,
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
        ev={"cp": ev} if ev else {},
        secondary=secondary or [],
        import_price=price,
        export_price=0.08,
        cost_basis={"bat": basis},
    )


def _run(tracker: SavingsTracker, samples: list[LiveSample]) -> list[SavingsSlot]:
    slots: list[SavingsSlot] = []
    for s in samples:
        slots += tracker.tick(s)
    return slots


class TestSettle:
    def test_import_and_export(self) -> None:
        assert settle(2.0, [], 0.30, 0.08) == pytest.approx(0.60)
        assert settle(-2.0, [], 0.30, 0.08) == pytest.approx(-0.16)

    def test_surplus_feeds_separately_billed_load_before_export(self) -> None:
        # 3 kWh surplus, heat pump needs 1 kWh at 0.18 → 2 kWh exported.
        assert settle(-3.0, [(1.0, 0.18)], 0.30, 0.08) == pytest.approx(-0.16)


class TestLedger:
    def test_average_cost_and_resize(self) -> None:
        led = Ledger(1.0, 0.20)
        led.add(4.0, 0.80)
        assert led.cost_per_kwh == pytest.approx(0.20)
        assert led.take(1.0) == pytest.approx(0.20)
        led.add(1.0, 0.40)  # 4 kWh at 0.80 + 1 kWh at 0.40
        assert led.cost_per_kwh == pytest.approx(0.24)
        led.resize(2.0)  # housekeeping keeps the average cost
        assert led.value_eur == pytest.approx(0.48)


class TestBatteries:
    def test_idle_house_saves_nothing(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        (slot,) = _run(tracker, [_sample(m, 1.0) for m in range(0, 62)])
        assert slot.hours == pytest.approx(1.0)
        assert slot.real_cost == pytest.approx(0.30)
        assert slot.saving == pytest.approx(0.0, abs=1e-9)

    def test_same_self_consumption_as_uncontrolled_saves_nothing(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        # The real battery covers the 1 kW load, as the uncontrolled one does.
        samples = [_sample(m, 0.0, storage_kw=-1.0, stored=5.0 - m / 60) for m in range(0, 62)]
        (slot, ) = _run(tracker, samples)[:1]
        assert slot.real_cost == pytest.approx(0.20, abs=1e-3)  # 1 kWh at its 0.20 basis
        assert slot.saving == pytest.approx(0.0, abs=1e-6)

    def test_night_charge_saves_nothing_then_the_peak_use_saves_the_spread(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        # Hour 1 at 0.20: 1 kW load, the real battery charges 4 kW from the grid.
        night = [
            _sample(m, 5.0, storage_kw=4.0, stored=MIN + 4.0 * m / 60, price=0.20)
            for m in range(0, 61)
        ]
        # Hour 2 at 0.40: the real battery covers the 1 kW load.
        peak = [
            _sample(60 + m, 0.0, storage_kw=-1.0, stored=5.0 - m / 60, price=0.40)
            for m in range(1, 62)
        ]
        first, second = _run(tracker, night + peak)[:2]
        assert first.real_bill == pytest.approx(1.0)        # 5 kWh at 0.20 …
        assert first.real_cost == pytest.approx(0.20)       # … 4 of them into the ledger
        assert first.saving == pytest.approx(0.0, abs=1e-9)
        assert second.real_cost == pytest.approx(0.20)      # 1 kWh at the 0.20 ledger cost
        assert second.uncontrolled_cost == pytest.approx(0.40)
        assert second.saving == pytest.approx(0.20)
        # What the batteries did, for the per-slot chart.
        assert first.real_battery_kwh == pytest.approx(4.0)
        assert first.uncontrolled_battery_kwh == pytest.approx(0.0)
        assert first.real_stored["bat"] == pytest.approx(5.0)
        assert first.uncontrolled_stored["bat"] == pytest.approx(MIN)
        assert first.import_price == pytest.approx(0.20)
        assert second.real_battery_kwh == pytest.approx(-1.0)

    def test_uncontrolled_battery_stores_pv_and_uses_it_later(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        # Hour 1: 2 kW PV surplus; the real battery stays idle and the house exports.
        sunny = [_sample(m, -2.0) for m in range(0, 61)]
        # Hour 2 at 0.40: 1 kW load, the real house imports it.
        evening = [_sample(60 + m, 1.0, price=0.40) for m in range(1, 62)]
        first, second = _run(tracker, sunny + evening)[:2]
        # Storing PV costs the feed-in it gives up — no saving or loss yet.
        assert first.saving == pytest.approx(0.0, abs=1e-9)
        assert tracker.state.unc["bat"].stored_kwh == pytest.approx(MIN + 2.0 - 1.0, abs=0.02)
        # Uncontrolled: 1 kWh from a ledger of 1 kWh at 0.20 + 2 kWh at 0.08.
        assert second.uncontrolled_cost == pytest.approx(0.36 / 3, abs=1e-6)
        assert second.saving == pytest.approx(0.12 - 0.40, abs=1e-6)

    def test_uncontrolled_battery_does_not_cover_the_separately_billed_load(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        hp = [(1.0, 0.18)]  # heat pump 1 kW on its own tariff
        samples = [_sample(m, 1.0, stored=5.0, secondary=hp) for m in range(0, 62)]
        (slot,) = _run(tracker, samples)[:1]
        assert slot.real_cost == pytest.approx(0.18)
        assert slot.uncontrolled_cost == pytest.approx(0.18)

    def test_standby_draw_is_household_load_in_both_houses(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        samples = [_sample(m, 1.028, storage_kw=0.028) for m in range(0, 62)]
        (slot,) = _run(tracker, samples)[:1]
        assert slot.real_bill == pytest.approx(slot.uncontrolled_bill)
        assert slot.saving == pytest.approx(0.0, abs=1e-9)

    def test_forced_recharge_below_min_soc_is_mirrored(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        # Sagged to 0.8 kWh (min 1.0); the inverter recharges at 0.4 kW.
        samples = [
            _sample(m, 0.4, storage_kw=0.4, stored=0.8 + 0.4 * m / 60, price=0.37)
            for m in range(0, 31)
        ] + [_sample(30 + m, 0.0, stored=1.0, price=0.37) for m in range(1, 32)]
        (slot,) = _run(tracker, samples)[:1]
        assert slot.saving == pytest.approx(0.0, abs=1e-9)
        assert tracker.state.unc["bat"].stored_kwh == pytest.approx(1.0, abs=1e-6)


class TestCars:
    def _plugged(self, kw: float) -> EvLive:
        return EvLive(plugged=True, power_kw=kw, max_kw=11.0)

    def test_uncontrolled_car_charges_as_late_as_possible(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        tracker.tick(_sample(0, 0.0, ev=self._plugged(0.0)))
        tracker.set_ev_goal("cp", 11.0, T0 + timedelta(hours=3))

        def price(m: int) -> float:
            return 0.20 if m <= 60 else 0.30 if m <= 120 else 0.40

        # The real car charges its 11 kWh in the first, cheapest hour.
        samples = [
            _sample(m, 11.0 if m <= 60 else 0.0, price=price(m),
                    ev=self._plugged(11.0 if m <= 60 else 0.0))
            for m in range(1, 182)
        ]
        first, second, third = _run(tracker, samples)[:3]
        # Charged energy is held until the uncontrolled car charges it too.
        assert first.real_bill == pytest.approx(2.20)
        assert first.saving == pytest.approx(0.0, abs=1e-9)
        assert second.saving == pytest.approx(0.0, abs=1e-9)
        # The uncontrolled car charges in the last hour before the deadline.
        assert third.uncontrolled_bill == pytest.approx(4.40)
        assert third.saving == pytest.approx(11.0 * (0.40 - 0.20))
        assert third.ev_saving == pytest.approx(third.saving)

    def test_energy_unmatched_at_unplug_is_neutral(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        tracker.tick(_sample(0, 0.0, ev=self._plugged(0.0)))
        tracker.set_ev_goal("cp", 2.0, T0 + timedelta(hours=10))
        samples = [_sample(m, 2.0, ev=self._plugged(2.0)) for m in range(1, 61)]
        samples += [_sample(60 + m, 0.0, ev=EvLive(False, 0.0, 11.0)) for m in range(1, 62)]
        first, second = _run(tracker, samples)[:2]
        assert first.real_bill == pytest.approx(0.60)
        assert first.saving == pytest.approx(0.0, abs=1e-9)
        assert second.saving == pytest.approx(0.0, abs=1e-9)
        assert tracker.state.evs == {}

    def test_uncontrolled_battery_discharges_into_the_car(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        # No charge plan: the uncontrolled car charges like the real one.
        # The real battery holds 5 kWh; the uncontrolled one covers the car.
        samples = [
            _sample(m, 2.0, stored=5.0, price=0.40, ev=self._plugged(2.0)) for m in range(0, 62)
        ]
        (slot,) = _run(tracker, samples)[:1]
        assert slot.real_cost == pytest.approx(0.80)          # 2 kWh at 0.40
        assert slot.uncontrolled_cost == pytest.approx(0.40)  # 2 kWh at the 0.20 ledger cost
        assert slot.ev_saving == pytest.approx(-0.40)


class TestHousekeeping:
    def test_resync_after_three_days(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        tracker.tick(_sample(0, -2.0, stored=5.0))
        tracker.state.unc["bat"].resize(3.0)
        tracker.tick(_sample(1, 1.0, stored=5.0))
        assert tracker.state.unc["bat"].stored_kwh != pytest.approx(5.0)
        tracker.state.last_resync["bat"] = (T0 - timedelta(days=3)).isoformat()
        tracker.tick(_sample(2, 1.0, stored=5.0))
        assert tracker.state.unc["bat"].stored_kwh == pytest.approx(5.0)

    def test_both_empty_is_a_resync_point(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=60)
        tracker.tick(_sample(0, 1.0, stored=MIN))
        tracker.state.unc["bat"].resize(MIN + 0.1)
        tracker.tick(_sample(1, 0.0, stored=MIN - 0.05))  # both idle near empty
        assert tracker.state.unc["bat"].stored_kwh == pytest.approx(MIN - 0.05)

    def test_long_gap_restarts_level_with_reality_and_books_nothing(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=15)
        slots = _run(tracker, [_sample(0, -2.0, stored=5.0), _sample(1, -2.0, stored=5.0)])
        assert tracker.state.unc["bat"].stored_kwh > 5.0
        slots += _run(tracker, [_sample(180, 1.0, stored=4.0)])
        assert tracker.state.unc["bat"].stored_kwh == pytest.approx(4.0)
        slots += _run(tracker, [_sample(181, 1.0, stored=4.0)])
        assert sum(s.hours for s in slots) == pytest.approx(1 / 60)

    def test_state_round_trips_through_json(self) -> None:
        tracker = SavingsTracker([BAT], step_minutes=15)
        tracker.tick(_sample(0, 0.0, ev=EvLive(True, 0.0, 11.0)))
        tracker.set_ev_goal("cp", 5.0, T0 + timedelta(hours=8))
        data = json.loads(json.dumps(tracker.state.to_dict()))
        assert SavingsState.from_dict(data) == tracker.state
        assert SavingsState.from_dict({"stored_kwh": {"bat": 1.0}}) is None  # old format


class TestDayAheadWindow:
    def test_day_opens_shortly_before_local_midnight(self) -> None:
        lead = timedelta(minutes=30)
        tomorrow = datetime.now().date() + timedelta(days=1)
        local_midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day).astimezone()
        next_midnight = datetime.fromordinal(tomorrow.toordinal() + 1).astimezone()
        assert day_ahead_window_end(local_midnight - timedelta(hours=5), lead) == local_midnight
        assert day_ahead_window_end(local_midnight - timedelta(minutes=10), lead) == next_midnight


class TestSavingsStorage:
    async def test_slots_round_trip_and_retention(self, tmp_path: Path) -> None:
        db = SqliteStorageBackend(tmp_path / "h.db")
        await db.start()
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            old = SavingsSlot(now - timedelta(days=401), 0.25, 0.1, 0.2, 0.1, 0.2, 0.0)
            new = SavingsSlot(now - timedelta(hours=1), 0.25, 0.10, 0.25, 0.30, 0.25, 0.05)
            await db.save_savings_slot(old)
            await db.save_savings_slot(new)
            # Legacy rows: kept a week for debugging.
            for age in (8, 2):
                await db._db.execute(
                    "INSERT INTO savings_actual (slot_start, hours, baseline_cost, actual_cost,"
                    " inventory_delta) VALUES (?, 0.25, 0, 0, 0)",
                    ((now - timedelta(days=age)).isoformat(timespec="seconds"),),
                )
            await db._db.commit()

            assert await db.purge_savings(now) == 2
            (row,) = await db.query_savings_slots(now - timedelta(days=500), now)
            assert row["saving"] == pytest.approx(0.15)
            assert row["ev_saving"] == pytest.approx(0.05)
            async with db._db.execute("SELECT COUNT(*) FROM savings_actual") as cur:
                assert (await cur.fetchone())[0] == 1
        finally:
            await db.stop()

    async def test_slot_detail_round_trips_and_old_table_is_migrated(self, tmp_path: Path) -> None:
        import sqlite3

        path = tmp_path / "h.db"
        with sqlite3.connect(path) as con:  # table as first released (#82)
            con.execute(
                "CREATE TABLE savings_slots (slot_start TEXT PRIMARY KEY, hours REAL NOT NULL,"
                " real_cost REAL NOT NULL, uncontrolled_cost REAL NOT NULL,"
                " real_bill REAL NOT NULL, uncontrolled_bill REAL NOT NULL,"
                " ev_saving REAL NOT NULL)"
            )
            con.execute(
                "INSERT INTO savings_slots VALUES (?, 0.25, 0.1, 0.2, 0.1, 0.2, 0)",
                (T0.isoformat(timespec="seconds"),),
            )
        db = SqliteStorageBackend(path)
        await db.start()
        try:
            slot = SavingsSlot(
                T0 + timedelta(minutes=15), 0.25, 0.1, 0.2, 0.1, 0.2, 0.0,
                real_battery_kwh=-0.2, uncontrolled_battery_kwh=0.3, real_ev_kwh=1.0,
                real_stored={"bat": 3.0, "b2": 1.0}, uncontrolled_stored={"bat": 2.0},
                price_hours=0.25 * 0.4,
            )
            await db.save_savings_slot(slot)
            old, new = await db.query_savings_slots(T0, T0 + timedelta(hours=1))
            assert old["real_stored_kwh"] is None and old["stored_detail"] is None
            assert new["real_battery_kwh"] == pytest.approx(-0.2)
            assert new["real_stored_kwh"] == pytest.approx(4.0)
            assert new["uncontrolled_stored_kwh"] == pytest.approx(2.0)
            assert new["import_price"] == pytest.approx(0.4)
            assert new["stored_detail"]["real"]["b2"] == pytest.approx(1.0)
        finally:
            await db.stop()

    async def test_state_round_trip(self, tmp_path: Path) -> None:
        db = SqliteStorageBackend(tmp_path / "h.db")
        await db.start()
        try:
            assert await db.load_savings_state() is None
            await db.save_savings_state(SavingsState().to_dict())
            saved_at, state = await db.load_savings_state()
            assert SavingsState.from_dict(state) == SavingsState()
            assert datetime.now(timezone.utc) - saved_at < timedelta(minutes=1)
        finally:
            await db.stop()


class TestServerWiring:
    """The Application glue: live sample → slot rows → API."""

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

            (row,) = await app._storage.query_savings_slots(T0, T0 + timedelta(hours=1))
            # Real: 1 kW household at 0.30 + 0.5 kW heat pump at 0.18, for 15 min.
            assert row["real_cost"] == pytest.approx(0.25 * (0.30 + 0.09))
            # Uncontrolled: its battery covers the household (0.20 ledger cost),
            # not the heat pump.
            assert row["uncontrolled_cost"] == pytest.approx(0.25 * (0.20 + 0.09))
            saved = (await app._storage.load_savings_state())[1]
            assert saved["unc"]["bat"]["stored_kwh"] < 5.0
        finally:
            await app._storage.stop()

    async def test_api_rolling_window_and_buckets(self, tmp_path: Path) -> None:
        import httpx

        app = await self._app(tmp_path)
        try:
            now = datetime.now(timezone.utc)
            for h in (1, 2, 30):
                await app._storage.save_savings_slot(
                    SavingsSlot(now - timedelta(hours=h), 0.25, 0.10, 0.30, 0.10, 0.30, 0.0)
                )
            transport = httpx.ASGITransport(app=app._build_api(), client=("127.0.0.1", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://ea") as c:
                day = (await c.get("/api/savings", params={"hours": 24})).json()
                week = (await c.get("/api/savings", params={"hours": 168})).json()
                bad = await c.get("/api/savings", params={"bucket": "month"})
            assert day["bucket"] == "hour" and len(day["buckets"]) == 2
            assert day["totals"]["real"] == pytest.approx(0.40)
            assert week["bucket"] == "day"
            assert week["totals"]["real"] == pytest.approx(0.60)
            assert day["all_time"]["real"] == pytest.approx(0.60)
            assert "real" in day["stored"] and "uncontrolled" in day["stored"]
            assert len(day["slots"]) == 2 and week["slots"] is None
            assert bad.status_code == 400
        finally:
            await app._storage.stop()


def test_kpi_sensor_is_a_monetary_total() -> None:
    (cfg,) = [cfg for _, cfg in _savings_discovery_configs()]
    assert cfg["unique_id"] == "ea_savings_real"
    assert cfg["device_class"] == "monetary"
    assert cfg["state_class"] == "total"

"""Execution log (core/execution_log.py), plan snapshots and their storage/API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nornwatt.core.execution_log import DeviceTick, ExecutionLog
from nornwatt.core.models import ControlIntent, EnergyPlan, PlanFlow
from nornwatt.storage.sqlite import SqliteStorageBackend

T0 = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)


def _tick(mode: str, planned: float | None, setpoint: float | None, measured: float | None):
    return {"zendure": DeviceTick(mode, planned, setpoint, measured)}


class TestExecutionLog:
    def test_slot_averages_are_time_weighted(self) -> None:
        log = ExecutionLog(step_minutes=15)
        finished = []
        # 10 min idle with the battery (wrongly) discharging, then 5 min a
        # planned discharge; one tick per minute, the last one in the next slot.
        for m in range(0, 16):
            tick = _tick("idle", 0.0, 0.0, -600.0) if m <= 10 else _tick("discharge", -300.0, -300.0, -290.0)
            finished += log.record(T0 + timedelta(minutes=m), tick, T0 - timedelta(minutes=8))
        (row,) = finished
        assert row.start == T0
        assert row.seconds == pytest.approx(900.0)
        assert row.mode == "idle"
        assert row.planned_w == pytest.approx(-300.0 * 5 / 15)
        assert row.measured_w == pytest.approx((-600.0 * 10 - 290.0 * 5) / 15)
        assert row.plan_created_at == T0 - timedelta(minutes=8)

    def test_missing_values_and_gaps(self) -> None:
        log = ExecutionLog(step_minutes=15)
        log.record(T0, _tick("no_plan", None, None, None), None)
        log.record(T0 + timedelta(minutes=1), _tick("no_plan", None, None, None), None)
        # A 20 min gap closes the open slot without bridging the gap.
        (row,) = log.record(T0 + timedelta(minutes=21), _tick("idle", 0.0, 0.0, 0.0), None)
        assert row.seconds == pytest.approx(60.0)
        assert row.planned_w is None and row.setpoint_w is None and row.measured_w is None
        assert row.mode == "no_plan"


def _plan(created_at: datetime) -> EnergyPlan:
    return EnergyPlan(
        created_at=created_at,
        step_minutes=15,
        intents=[
            ControlIntent(device_id="zendure", timestep=T0 + timedelta(minutes=15 * i), power_kw=-0.2 * i)
            for i in range(4)
        ],
        flows=[
            PlanFlow(
                timestep=T0 + timedelta(minutes=15 * i), pv_kw=0.0, grid_import_kw=1.0,
                grid_export_kw=0.0, hours=0.25, load_kw=1.0, stored_kwh={"zendure": 3.0},
            )
            for i in range(4)
        ],
    )


class TestStorage:
    async def test_plan_snapshots_round_trip_and_purge(self, tmp_path: Path) -> None:
        db = SqliteStorageBackend(tmp_path / "h.db")
        await db.start()
        try:
            day_ahead = _plan(T0 - timedelta(hours=6))
            later = _plan(T0 - timedelta(hours=1))
            await db.save_plan_snapshot(day_ahead, day_ahead=True)
            await db.save_plan_snapshot(later)
            listed = await db.list_plan_snapshots(T0 - timedelta(days=1), T0)
            assert [s["day_ahead"] for s in listed] == [True, False]
            assert await db.load_plan_snapshot(T0) == later
            assert await db.load_plan_snapshot(T0, day_ahead_only=True) == day_ahead
            assert await db.load_plan_snapshot(T0 - timedelta(hours=7)) is None

            await db.purge_before(T0 - timedelta(hours=2))
            assert [s["created_at"] for s in await db.list_plan_snapshots(T0 - timedelta(days=1), T0)] == [
                (T0 - timedelta(hours=1)).isoformat()
            ]
        finally:
            await db.stop()

    async def test_execution_rows_round_trip(self, tmp_path: Path) -> None:
        log = ExecutionLog(step_minutes=15)
        rows = []
        for m in range(0, 16):
            rows += log.record(T0 + timedelta(minutes=m), _tick("idle", 0.0, 0.0, -600.0), T0)
        db = SqliteStorageBackend(tmp_path / "h.db")
        await db.start()
        try:
            await db.save_execution_slots(rows)
            (row,) = await db.query_execution(T0, T0 + timedelta(hours=1), ["zendure"])
            assert row["mode"] == "idle"
            assert row["measured_w"] == pytest.approx(-600.0)
            assert await db.query_execution(T0, T0 + timedelta(hours=1), ["sma_battery"]) == []
        finally:
            await db.stop()


class TestApi:
    async def test_snapshot_and_execution_endpoints(self, tmp_path: Path) -> None:
        import httpx

        from nornwatt.core.config import AppConfig
        from nornwatt.server import Application

        app = Application.__new__(Application)
        app._cfg = AppConfig()
        app._storage = SqliteStorageBackend(tmp_path / "h.db")
        await app._storage.start()
        try:
            local_day = (T0 + timedelta(days=1)).astimezone().date()
            midnight = datetime(local_day.year, local_day.month, local_day.day).astimezone(timezone.utc)
            await app._storage.save_plan_snapshot(_plan(midnight - timedelta(minutes=26)), day_ahead=True)
            await app._storage.save_plan_snapshot(_plan(midnight + timedelta(hours=3)))
            log = ExecutionLog(step_minutes=15)
            rows = []
            for m in range(0, 16):
                rows += log.record(T0 + timedelta(minutes=m), _tick("idle", 0.0, 0.0, -600.0), T0)
            await app._storage.save_execution_slots(rows)

            transport = httpx.ASGITransport(app=app._build_api(), client=("127.0.0.1", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://ea") as c:
                r = await c.get("/api/plan/snapshot", params={"day_ahead_date": local_day.isoformat()})
                assert r.status_code == 200, r.text
                body = r.json()
                assert datetime.fromisoformat(body["created_at"]) == midnight - timedelta(minutes=26)
                assert len(body["intents"]) == 4 and body["flows"][0]["load_kw"] == 1.0

                r = await c.get("/api/plan/snapshot", params={
                    "at": (midnight + timedelta(hours=4)).isoformat(),
                    "start": (T0 + timedelta(minutes=15)).isoformat(),
                    "end": (T0 + timedelta(minutes=45)).isoformat(),
                })
                assert len(r.json()["intents"]) == 2

                assert (await c.get("/api/plan/snapshot")).status_code == 400

                r = await c.get("/api/execution", params={
                    "start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat(),
                })
                (row,) = r.json()["slots"]
                assert row["device_id"] == "zendure" and row["mode"] == "idle"
        finally:
            await app._storage.stop()

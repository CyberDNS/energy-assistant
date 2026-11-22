"""The vehicle API: /api/vehicles, per-vehicle routes, the guest mode."""

from __future__ import annotations

import httpx
from helpers.fleet_app import fleet_app


def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app._build_api(), client=("127.0.0.1", 1))
    return httpx.AsyncClient(transport=transport, base_url="http://ea")


def _app():
    from energy_assistant.core.config import AppConfig

    app = fleet_app()
    app._cfg = AppConfig()
    app._ev_weekly_plans = {}
    app._ev_day_overrides = {}
    app._last_ev_goals = []
    app._ev_feasibility_margin = 1.2
    return app


async def test_vehicles_list_uses_vehicle_and_charger_ids() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.get("/api/vehicles")
        assert r.status_code == 200, r.text
        (v,) = r.json()
        assert v["vehicle_id"] == "ev1"
        assert v["charger_id"] is None  # away: no session
        assert "asset_id" not in v and "device_id" not in v
        assert v["charger_status"]["state"] == "default"
        assert (await c.get("/api/ev")).status_code == 404


async def test_per_vehicle_routes() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.get("/api/vehicles/ev1/plan")
        assert r.status_code == 200, r.text
        assert r.json()["vehicle_id"] == "ev1"
        assert len(r.json()["weekly"]) == 7
        assert (await c.get("/api/vehicles/nope/plan")).status_code == 404

        r = await c.put("/api/vehicles/ev1/charger", params={"charger_id": "none"})
        assert r.status_code == 200, r.text
        r = await c.put("/api/vehicles/guest/charger", params={"charger_id": "wallbox"})
        assert r.status_code == 200, r.text
        assert app._fleet.sessions.next_vehicle == {"wallbox": "guest"}

        r = await c.put("/api/guest/mode", params={"mode": "instant"})
        assert r.status_code == 200, r.text
        assert app._fleet.guest_mode("wallbox") == "instant"


async def test_orphaned_stored_data_is_reported(tmp_path, caplog) -> None:
    from energy_assistant.core.models import StorageConstraints
    from energy_assistant.storage.sqlite import SqliteStorageBackend

    db = SqliteStorageBackend(tmp_path / "h.db")
    await db.start()
    try:
        for device_id in ("bat", "wallbox", "aquarium_cooler"):
            await db._db.execute(
                "INSERT INTO ledger_state VALUES (?, 0.2, 1.0, '2026-01-01T00:00:00+00:00')",
                (device_id,))
        for asset_id in ("ev1", "ev_old"):
            await db._db.execute(
                "INSERT INTO ev_weekly_plan VALUES (?, 1, 1, 80, '07:00')", (asset_id,))
        await db._db.commit()

        app = _app()
        app._storage = db
        app._storage_constraints = [StorageConstraints(
            device_id="bat", capacity_kwh=5.0, max_charge_kw=2.0, max_discharge_kw=2.0)]
        await app._report_orphaned_data()
    finally:
        await db.stop()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        "Stored data without a matching device: ev_weekly_plan: ev_old",
        "Stored data without a matching device: ledger_state: aquarium_cooler, wallbox",
    ]

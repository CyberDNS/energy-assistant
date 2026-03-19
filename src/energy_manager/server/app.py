"""
FastAPI application for the Energy Assistant web UI and REST API.

Usage (from run_controller.py)::

    from energy_manager.server.app import create_app
    app = create_app(state_ref)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

Endpoints
---------
GET /              → Single-page HTML dashboard
GET /api/state     → Full JSON state snapshot
GET /events        → Server-Sent Events stream (2 s cadence)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from energy_manager.server.models import (
    BatteryCard,
    GridCard,
    HomePowerCard,
    IntegrationCard,
    ScheduleSlot,
    StateResponse,
)

_STATIC = Path(__file__).parent / "static"


def _build_response(state_ref: Any) -> StateResponse:
    """Serialise the mutable ControllerState into a StateResponse."""
    now = datetime.now(timezone.utc)

    # --- Batteries ----------------------------------------------------------
    batteries: list[BatteryCard] = []
    if state_ref.last_zendure_state is not None:
        s = state_ref.last_zendure_state
        batteries.append(BatteryCard(
            device_id="zendure",
            soc_pct=s.soc_pct,
            power_w=s.power_w,
            controllable=True,
        ))
    if state_ref.last_sma_state is not None:
        s = state_ref.last_sma_state
        batteries.append(BatteryCard(
            device_id="sma_battery",
            soc_pct=s.soc_pct,
            power_w=s.power_w,
            controllable=False,
        ))

    # --- Home power ---------------------------------------------------------
    home_power: HomePowerCard | None = None
    if state_ref.last_home_power_state is not None:
        hp = state_ref.last_home_power_state
        home_power = HomePowerCard(
            household_w=hp.power_w or 0.0,
            overflow_w=hp.extra.get("overflow_w") or 0.0,
            cars_w=hp.extra.get("cars_w") or 0.0,
            pv_w=hp.extra.get("pv_w") or 0.0,
        )

    # --- Grid meter (SMA Energy Manager / Tibber / MT175) ------------------
    grid: GridCard | None = None
    if (
        state_ref.last_sma_em_state is not None
        or state_ref.last_tibber_live_state is not None
        or state_ref.last_mt175_state is not None
    ):
        gm = state_ref.last_sma_em_state
        tl = state_ref.last_tibber_live_state
        mt = state_ref.last_mt175_state
        grid = GridCard(
            import_w=gm.extra.get("import_w") if gm else None,
            export_w=gm.extra.get("export_w") if gm else None,
            net_w=gm.power_w if gm else None,
            tibber_net_w=tl.power_w if tl else None,
            mt175_net_w=mt.power_w if mt else None,
        )

    # --- Schedule -----------------------------------------------------------
    schedule: list[ScheduleSlot] = []
    if state_ref.plan is not None:
        for action in state_ref.plan.actions:
            slot_end = action.scheduled_at + timedelta(hours=1)
            schedule.append(ScheduleSlot(
                hour_iso=action.scheduled_at.isoformat(),
                planned_w=float(action.value),
                active=action.scheduled_at <= now < slot_end,
            ))

    return StateResponse(
        timestamp=now.isoformat(),
        mode=state_ref.last_mode,
        target_w=state_ref.last_target_w,
        maintenance_mode=state_ref.maintenance_mode,
        batteries=batteries,
        home_power=home_power,
        grid=grid,
        schedule=schedule,
        integrations=[
            IntegrationCard(
                name=s.name,
                power_w=s.power_w,
                power_import_w=s.power_import_w,
                power_export_w=s.power_export_w,
            )
            for s in (state_ref.registry.all_states().values()
                       if getattr(state_ref, "registry", None) else [])
        ],
    )


def create_app(state_ref: Any) -> FastAPI:
    app = FastAPI(title="Energy Assistant")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((_STATIC / "index.html").read_text())

    @app.get("/api/state", response_model=StateResponse)
    def get_state() -> StateResponse:
        return _build_response(state_ref)

    @app.get("/events", include_in_schema=False)
    async def events() -> StreamingResponse:
        async def generate():
            try:
                while True:
                    data = _build_response(state_ref).model_dump_json()
                    yield f"data: {data}\n\n"
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                pass

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app

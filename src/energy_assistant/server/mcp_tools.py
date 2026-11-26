"""Read-only MCP server over the REST API, for debugging the live installation.

Every tool is a thin wrapper around a ``GET`` endpoint, served in-process
through the FastAPI app itself, so the MCP view can never drift from what the
API and the web UI show — and can never change anything.  Access control is
the one of the REST API (see ``remote_access``).

Times are ISO 8601; without an offset they are read as UTC.  All money is €,
energy kWh, power W unless a field name says otherwise.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .remote_access import MCP_PATH

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

_INSTRUCTIONS = """\
Read-only view of a running Energy Assistant (home energy optimizer: batteries,
EV chargers, PV, dynamic tariffs). Nothing here changes the installation.

Good starting points:
- get_status: live grid power, prices, device states, current setpoints.
- get_plan: the active optimizer plan (per-slot flows, SoC, prices, intents),
  with a reason per device and slot (why it charges, discharges or holds).
- get_savings / get_savings_slots: savings against a simulated uncontrolled
  house, per rolling window or per accounting slot with all cost components.
- get_history: measured power/SoC per 5 minutes (kept ~2 weeks) or per hour
  (kept for good), and the battery-ledger history (kept ~8 days).
- get_execution: per slot and device, the plan's intent vs. the setpoint sent
  vs. the measured power — did the control loop follow the plan?
- list_plan_snapshots / get_plan_snapshot: every past plan (kept ~8 days),
  including the day-ahead plan of each day (made shortly before midnight).
Savings: saving = uncontrolled cost - real cost. Both houses run on the same
measured load, PV and prices; the uncontrolled one charges batteries from PV
surplus only, discharges them to cover load, and charges cars as late as
possible to reach their charge plan. Battery energy is costed when used, at
its own house's ledger cost (charging moves the cost into the ledger), so
charging slots save nothing and the saving shows when stored energy is used.
So a house holding more stored energy looks worse until it uses it:
'real_with_stored' adds the change of 'stored_position' (usable stored energy
valued at the mean import price of the last 24 h minus its ledger cost, real
minus uncontrolled) over the window. Battery wear is a cost in both houses:
each kWh stored costs purchase price / (cycle life x capacity), booked when
charged; component 'battery_wear' is uncontrolled minus real wear, and the
totals carry 'real_wear' / 'uncontrolled_wear'. With power charges on the
default tariff (15-minute import above a threshold, the month's peak), both
houses pay them when an interval ends; component 'power_charges'.
get_status 'power_charges': running interval, month peak, allowed import.
"""


def build_mcp_server(api: FastAPI) -> MCPServer:
    """Create the MCP server; its tools query *api* in-process."""
    mcp = MCPServer(name="energy-assistant", instructions=_INSTRUCTIONS)
    transport = httpx.ASGITransport(app=api)

    async def get(path: str, **params: Any) -> dict[str, Any]:
        query = {k: v for k, v in params.items() if v not in (None, "")}
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp.internal") as client:
            response = await client.get(path, params=query, timeout=60.0)
        if response.status_code >= 400:
            raise ValueError(f"GET {path} failed ({response.status_code}): {response.text[:500]}")
        data = response.json()
        return data if isinstance(data, dict) else {"items": data}

    @mcp.tool(annotations=_READ_ONLY)
    async def get_status() -> dict[str, Any]:
        """Live snapshot: grid power, current prices, every device's state
        (power, SoC, availability), active setpoints and the battery ledger."""
        return await get("/api/status")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_version() -> dict[str, Any]:
        """Version of the running build: release, channel (``dev`` for builds
        of main) and git commit."""
        return await get("/api/version")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_plan() -> dict[str, Any]:
        """The active optimizer plan: per-slot flows (load, PV, storage, EV,
        stored energy, prices, marginal price), predicted economics and the
        control intents, each with the reason for it ('reason'), the
        constraints limiting it ('binding_constraints') and, for batteries,
        the value of one more kWh stored ('energy_value_eur_per_kwh')."""
        return await get("/api/plan", detail="true")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_forecast() -> dict[str, Any]:
        """The aligned forecast series the optimizer plans on: import/export
        prices, PV and consumption per slot."""
        return await get("/api/forecast")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_savings(hours: float = 720.0, bucket: str | None = None) -> dict[str, Any]:
        """Savings over the rolling window of the last *hours*: totals, one row
        per *bucket* ('hour', 'day' or 'week', local time; default by window
        length), energy still stored in both houses and all-time totals.
        'real' is the saving (uncontrolled cost - real cost); 'real_with_stored'
        also counts the change of the saving still stored in the batteries."""
        return await get("/api/savings", hours=hours, bucket=bucket)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_savings_slots(start: str, end: str | None = None) -> dict[str, Any]:
        """Per accounting slot in [start, end) (max 31 days): ledger-based
        cost of the real and the uncontrolled house, both plain grid bills,
        the saving and the part booked for cars, and per slot the saving
        still stored in the batteries ('stored_position', real minus
        uncontrolled, at 'reference_price'). Use it to find *when* savings
        were made or lost."""
        return await get("/api/savings/slots", start=start, end=end)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_history(
        start: str | None = None,
        end: str | None = None,
        hours: float = 24.0,
        device_ids: str = "",
        resolution: str = "auto",
    ) -> dict[str, Any]:
        """Mean power (W) and last SoC (%) per device and bucket, plus the
        battery ledger (cost basis, stored kWh). Window: [start, end] if start
        is given, otherwise the last *hours*. device_ids: comma-separated;
        default is storage, meters and PV. resolution: "5m" (kept ~2 weeks),
        "1h" (kept for good) or "auto" (5m up to 3 days, else 1h). pos_w /
        neg_w are the means of the positive and negative part of the power."""
        return await get("/api/history", start=start, end=end, hours=hours,
                         device_ids=device_ids, resolution=resolution)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_execution(
        start: str, end: str | None = None, device_ids: str = ""
    ) -> dict[str, Any]:
        """Control execution log in [start, end) (max 8 days): per accounting
        slot and device the dominant intent mode, time-averaged planned power,
        setpoint sent and measured power (W, + = charge/consume).  observed =
        the device was in observe mode: setpoint_w is what would have been sent.
        device_ids: comma-separated; default all controlled devices."""
        return await get("/api/execution", start=start, end=end, device_ids=device_ids)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_plan_snapshots(start: str, end: str | None = None) -> dict[str, Any]:
        """Stored plans made in [start, end): creation time and whether it is
        the day-ahead plan (made shortly before midnight for the coming day)."""
        return await get("/api/plan/snapshots", start=start, end=end)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_plan_snapshot(
        at: str | None = None,
        day_ahead_date: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """A stored plan in the get_plan format: the last plan made at or
        before *at*, or the day-ahead plan of local day *day_ahead_date*
        (YYYY-MM-DD). Give exactly one of them. start/end limit the flows and
        intents returned (a whole plan is large)."""
        return await get(
            "/api/plan/snapshot", at=at, day_ahead_date=day_ahead_date, start=start, end=end
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_ledger() -> dict[str, Any]:
        """Battery ledger per storage device: stored energy and its cost basis
        (€/kWh) as used for the savings inventory valuation."""
        return await get("/api/ledger")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_config() -> dict[str, Any]:
        """Device ids, categories and connections, and the storage constraints (capacity, SoC
        limits, efficiencies, power limits). Contains no secrets."""
        return await get("/api/config")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_vehicles() -> dict[str, Any]:
        """Vehicles: where each one is (charger and since when, or away), its
        SoC (the car's last report, or estimated from the charger's energy
        since then), charging goal and its status."""
        return await get("/api/vehicles")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_vehicle_plan(vehicle_id: str) -> dict[str, Any]:
        """One vehicle's weekly charging plan, day overrides and the resulting
        7-day targets."""
        return await get(f"/api/vehicles/{vehicle_id}/plan")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_chargers() -> dict[str, Any]:
        """EV chargers and the car at each one: a configured vehicle, a guest,
        or none; how it was assigned (default on plug-in or manual)."""
        return await get("/api/chargers")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_controllable() -> dict[str, Any]:
        """Controllable devices and what the control loop currently commands."""
        return await get("/api/controllable")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_learned_forecasts() -> dict[str, Any]:
        """Every learned model, each with a ``category``: ``consumption``
        (daily-energy forecasts, e.g. heat pump, household baseline), ``pv``
        (the PV forecast learned from the measured production: orientation,
        tilt, power, inverter limit, shading, snow, the correction by the last
        hour, measured vs. model per day), ``thermal`` (min/max devices such
        as the aquarium cooler) and
        ``storage`` (battery round-trip efficiency: configured vs. learned,
        the closed cycles it is based on, and whether the plan uses it)."""
        return await get("/api/learned")

    @mcp.tool(annotations=_READ_ONLY)
    async def get_plan_inputs() -> dict[str, Any]:
        """What the optimizer would receive on its next run: device states,
        forecast series lengths, effective horizon and step count."""
        return await get("/api/debug/plan_inputs")

    return mcp


def mount_mcp_server(api: FastAPI) -> MCPServer:
    """Serve the MCP server at ``POST /mcp`` on *api*.

    Stateless JSON responses: every call stands alone, so no session state
    survives restarts or needs cleaning up.  The caller must run
    ``mcp.session_manager.run()`` for the app's lifetime (its lifespan).
    """
    mcp = build_mcp_server(api)
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",  # access is checked by ApiAccessMiddleware, not by Host header
    )
    api.router.routes.extend(app.routes)
    return mcp

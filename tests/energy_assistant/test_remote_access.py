"""Token-guarded remote API access and the read-only MCP endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from energy_assistant.core.savings import ActualSlot, PredictedSlot
from energy_assistant.server.remote_access import HA_INGRESS_PROXY, ApiAccessMiddleware
from energy_assistant.storage.sqlite import SqliteStorageBackend

LAN_CLIENT = ("192.168.1.100", 50000)
TOKEN = "s3cret-token"
T0 = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def _guarded_app(token: str | None) -> FastAPI:
    api = FastAPI()

    @api.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @api.get("/api/thing")
    async def read() -> dict:
        return {"read": True}

    @api.post("/api/thing")
    async def write() -> dict:
        return {"written": True}

    @api.post("/mcp")
    async def mcp() -> dict:
        return {"mcp": True}

    api.add_middleware(ApiAccessMiddleware, token=token)
    return api


def _client(app, client=LAN_CLIENT, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client),
        base_url="http://ea", headers=headers,
    )


class TestApiAccessMiddleware:
    async def test_without_a_configured_token_nothing_changes(self) -> None:
        async with _client(_guarded_app(None)) as c:
            assert (await c.get("/api/thing")).status_code == 200
            assert (await c.post("/api/thing")).status_code == 200

    async def test_remote_requests_need_the_token(self) -> None:
        app = _guarded_app(TOKEN)
        async with _client(app) as c:
            r = await c.get("/api/thing")
            assert r.status_code == 401
            assert r.headers["www-authenticate"] == "Bearer"
            assert (await c.get("/health")).status_code == 200
        async with _client(app, token="wrong") as c:
            assert (await c.get("/api/thing")).status_code == 401

    async def test_the_token_grants_reads_and_mcp_but_no_writes(self) -> None:
        async with _client(_guarded_app(TOKEN), token=TOKEN) as c:
            assert (await c.get("/api/thing")).json() == {"read": True}
            assert (await c.post("/mcp")).json() == {"mcp": True}
            r = await c.post("/api/thing")
            assert r.status_code == 403

    @pytest.mark.parametrize("host", [HA_INGRESS_PROXY, "127.0.0.1"])
    async def test_ingress_and_localhost_keep_full_access(self, host: str) -> None:
        async with _client(_guarded_app(TOKEN), client=(host, 1234)) as c:
            assert (await c.post("/api/thing")).json() == {"written": True}


class TestMcpEndpoint:
    """The real Application API: token check, new endpoints, MCP tools."""

    async def _app(self, tmp_path: Path):
        from energy_assistant.core.config import AppConfig
        from energy_assistant.server import Application

        app = Application.__new__(Application)
        app._cfg = AppConfig(server={"api_token": TOKEN})
        app._storage = SqliteStorageBackend(tmp_path / "h.db")
        await app._storage.start()
        await app._storage.save_savings_predictions(
            "day_ahead", [PredictedSlot(T0, 0.25, 0.30, 0.10, 0.05)], T0, overwrite=False
        )
        await app._storage.save_savings_actual(ActualSlot(T0, 0.25, 0.40, 0.35, -0.02))
        return app

    async def test_savings_slots_endpoint_has_all_components(self, tmp_path: Path) -> None:
        app = await self._app(tmp_path)
        try:
            async with _client(app._build_api(), token=TOKEN) as c:
                r = await c.get("/api/savings/slots", params={"start": "2026-11-16T00:00:00"})
            body = r.json()
            (slot,) = body["slots"]
            assert slot["real"] == pytest.approx(0.03)
            assert slot["inventory_delta"] == pytest.approx(-0.02)
            assert slot["day_ahead"]["saving"] == pytest.approx(0.25)
            assert slot["latest"] is None
            assert body["totals"]["deviation"] == pytest.approx(0.03 - 0.25)
        finally:
            await app._storage.stop()

    async def test_mcp_tools_answer_with_the_api_data(self, tmp_path: Path) -> None:
        app = await self._app(tmp_path)
        try:
            api = app._build_api()
            async with api.router.lifespan_context(api), _client(api, token=TOKEN) as c:
                headers = {"Accept": "application/json, text/event-stream"}

                async def rpc(method: str, params: dict) -> dict:
                    r = await c.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
                    })
                    assert r.status_code == 200, r.text
                    return r.json()["result"]

                await rpc("initialize", {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                })
                tools = {t["name"]: t for t in (await rpc("tools/list", {}))["tools"]}
                assert {"get_status", "get_plan", "get_savings_slots", "get_history"} <= set(tools)
                assert all(t["annotations"]["readOnlyHint"] for t in tools.values())

                result = await rpc("tools/call", {
                    "name": "get_savings_slots",
                    "arguments": {"start": "2026-11-16T00:00:00Z", "end": "2026-11-17T00:00:00Z"},
                })
                data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
                assert data["slots"][0]["day_ahead"]["plan_cost"] == pytest.approx(0.10)
        finally:
            await app._storage.stop()

    async def test_mcp_needs_the_token_from_outside(self, tmp_path: Path) -> None:
        app = await self._app(tmp_path)
        try:
            async with _client(app._build_api()) as c:
                assert (await c.post("/mcp", json={})).status_code == 401
        finally:
            await app._storage.stop()

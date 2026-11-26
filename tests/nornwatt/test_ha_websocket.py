"""HAClient: websocket state cache, service calls and REST fallback."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from helpers.fake_ha_websocket import FakeHAWebSocketServer, until

from nornwatt.plugins._homeassistant import client as client_mod
from nornwatt.plugins._homeassistant import websocket as ws_mod
from nornwatt.plugins._homeassistant.client import HAClient

T0 = 1795500000.0  # epoch seconds, as HA sends lc/lu


def _no_rest(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected REST request {request.method} {request.url.path}")


def _client(url: str, token: str = "token", rest=_no_rest) -> HAClient:
    client = HAClient(url, token)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(rest), base_url=url)
    return client


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "_STARTUP_WAIT_S", 1.0)
    monkeypatch.setattr(ws_mod, "_BACKOFF_S", (0.05,))
    monkeypatch.setattr(ws_mod, "_AUTH_FAILED_BACKOFF_S", 0.05)


async def test_reads_come_from_the_cache_without_rest() -> None:
    states = {
        "sensor.grid": {"s": "1234.5", "a": {"unit_of_measurement": "W",
                                             "friendly_name": "Grid"}, "lc": T0},
        "switch.plug": {"s": "on", "a": {}, "lc": T0, "lu": T0 + 5},
    }
    async with FakeHAWebSocketServer(states) as server:
        client = _client(server.url)
        try:
            assert await client.get_entity_state("sensor.grid") == "1234.5"
            assert await client.get_entity_state("sensor.unknown") is None
            assert await client.get_entity("sensor.unknown") is None
            plug = await client.get_entity("switch.plug")
            assert plug is not None and plug.state == "on"
            assert plug.last_changed == datetime.fromtimestamp(T0, tz=UTC)
            assert plug.last_updated == plug.last_reported == datetime.fromtimestamp(T0 + 5, tz=UTC)
            assert await client.list_entities() == [
                {"entity_id": "sensor.grid", "name": "Grid"},
                {"entity_id": "switch.plug", "name": ""},
            ]
            assert client.cache_ready
            assert server.connections == 1
        finally:
            await client.close()


async def test_changes_and_removals_update_the_cache() -> None:
    states = {
        "sensor.p": {"s": "1", "a": {"unit_of_measurement": "W", "icon": "mdi:x"},
                     "lc": T0, "lu": T0 + 1},
        "sensor.gone": {"s": "1", "a": {}, "lc": T0},
    }
    async with FakeHAWebSocketServer(states) as server:
        client = _client(server.url)
        try:
            await client.start()
            await server.push({"c": {"sensor.p": {"+": {"s": "2", "lc": T0 + 10,
                                                        "a": {"friendly_name": "P"}},
                                                  "-": {"a": ["icon"]}}}})
            await server.push({"r": ["sensor.gone"]})
            await server.push({"a": {"sensor.new": {"s": "7", "a": {}, "lc": T0}}})
            await until(lambda: client._ws.get("sensor.new") is not None)

            p = await client.get_entity("sensor.p")
            assert p.state == "2"
            assert p.attributes == {"unit_of_measurement": "W", "friendly_name": "P"}
            # a new last_changed resets last_updated to it
            assert p.last_updated == datetime.fromtimestamp(T0 + 10, tz=UTC)
            await server.push({"c": {"sensor.p": {"+": {"lu": T0 + 20, "a": {"x": 1}}}}})
            await until(lambda: client._ws.get("sensor.p").attributes.get("x") == 1)
            p = await client.get_entity("sensor.p")
            assert p.last_changed == datetime.fromtimestamp(T0 + 10, tz=UTC)
            assert p.last_updated == datetime.fromtimestamp(T0 + 20, tz=UTC)
            assert await client.get_entity_state("sensor.gone") is None
            assert await client.get_entity_state("sensor.new") == "7"
        finally:
            await client.close()


async def test_service_calls_go_over_the_websocket() -> None:
    async with FakeHAWebSocketServer({}) as server:
        client = _client(server.url)
        try:
            await client.start()
            await client.call_service("switch", "turn_on", {"entity_id": "switch.plug"})
            (call,) = server.service_calls
            assert (call["domain"], call["service"], call["service_data"]) == (
                "switch", "turn_on", {"entity_id": "switch.plug"})

            server.service_error = {"code": "not_found", "message": "Service not found"}
            with pytest.raises(ws_mod.HAWebSocketError, match="not_found"):
                await client.call_service("nope", "nope", {})
        finally:
            await client.close()


async def test_energy_prefs_use_the_open_connection() -> None:
    async with FakeHAWebSocketServer({}) as server:
        server.energy_prefs = {"device_consumption": [{"stat_consumption": "sensor.x"}]}
        client = _client(server.url)
        try:
            await client.start()
            assert await client.get_energy_prefs() == server.energy_prefs
            assert server.connections == 1
        finally:
            await client.close()


async def test_reconnects_and_resyncs_after_a_dropped_connection() -> None:
    async with FakeHAWebSocketServer({"sensor.p": {"s": "1", "a": {}, "lc": T0}}) as server:
        client = _client(server.url)
        try:
            await client.start()
            server.states = {"sensor.p": {"s": "5", "a": {}, "lc": T0 + 60}}
            await server.drop_connections()
            await until(lambda: server.connections == 2 and client.cache_ready)
            assert await client.get_entity_state("sensor.p") == "5"
        finally:
            await client.close()


async def test_falls_back_to_rest_while_the_websocket_is_unavailable() -> None:
    seen: list[str] = []

    def rest(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/states/sensor.p":
            return httpx.Response(200, json={"entity_id": "sensor.p", "state": "3",
                                             "attributes": {}})
        return httpx.Response(200, json=[])

    async with FakeHAWebSocketServer({}, token="other") as server:  # token rejected
        client = _client(server.url, rest=rest)
        try:
            assert await client.get_entity_state("sensor.p") == "3"
            await client.call_service("switch", "turn_off", {"entity_id": "switch.plug"})
            assert seen == ["GET /api/states/sensor.p", "POST /api/services/switch/turn_off"]
            assert not client.cache_ready
            assert server.service_calls == []
        finally:
            await client.close()


async def test_close_stops_the_connection() -> None:
    async with FakeHAWebSocketServer({}) as server:
        client = _client(server.url)
        await client.start()
        ws = client._ws
        await client.close()
        assert not ws.connected and not ws.ready.is_set()
        assert ws._task is None


async def test_entity_catalog_for_the_config_ui() -> None:
    states = {
        "select.bat_mode": {"s": "Self", "lc": T0, "a": {
            "friendly_name": "Battery mode", "options": ["Self", "Charge"], "icon": "mdi:x"}},
        "climate.office": {"s": "heat", "lc": T0, "a": {
            "friendly_name": "Office", "hvac_modes": ["off", "heat"], "preset_modes": ["eco"],
            "current_temperature": 21.5, "temperature": 21}},
        "sensor.power": {"s": "512", "lc": T0, "a": {
            "friendly_name": "Power", "unit_of_measurement": "W", "device_class": "power"}},
    }
    async with FakeHAWebSocketServer(states) as server:
        server.entity_registry = [{"ei": "climate.office", "ai": "office"},
                                  {"ei": "sensor.power", "di": "dev1"}]
        server.device_registry = [{"id": "dev1", "area_id": "garage"}]
        server.area_registry = [{"area_id": "office", "name": "Office"},
                                {"area_id": "garage", "name": "Garage"}]
        client = _client(server.url)
        try:
            catalog = {e["entity_id"]: e for e in await client.entity_catalog()}
        finally:
            await client.close()
    assert catalog["select.bat_mode"]["options"] == {"options": ["Self", "Charge"]}
    assert catalog["select.bat_mode"]["area"] is None
    office = catalog["climate.office"]
    assert office["area"] == "Office"
    assert office["options"] == {"hvac_modes": ["off", "heat"], "preset_modes": ["eco"]}
    assert "current_temperature" in office["attributes"]
    assert "friendly_name" not in office["attributes"]
    power = catalog["sensor.power"]
    assert (power["area"], power["state"], power["unit"], power["device_class"]) == (
        "Garage", "512", "W", "power")


async def test_entity_catalog_without_registry_access() -> None:
    async with FakeHAWebSocketServer({"sensor.p": {"s": "1", "lc": T0, "a": {}}}) as server:
        client = _client(server.url)
        try:
            (entry,) = await client.entity_catalog()
        finally:
            await client.close()
    assert entry["entity_id"] == "sensor.p" and entry["area"] is None

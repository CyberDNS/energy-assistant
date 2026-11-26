"""Zero-config start: Nornwatt always starts, whatever config.yaml holds.

A config mistake never ends the process.  With problems the app runs in
"configuration needed" state — the UI, the config API and the Home Assistant
connection run, nothing is planned or controlled — and the problems are
fixed through the config API, which applies the fixed file at once.

One test per situation: no file, empty file, invalid YAML, schema errors,
a missing secret, an unreachable device, a failing build; then the fixes via
the API, and that a running configuration can never be broken from the UI.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from nornwatt.config.yaml import (
    SUPERVISOR_CORE_URL,
    SUPERVISOR_CORE_WS_URL,
    YamlConfigLoader,
    fallback_config,
)
from nornwatt.server import Application

GOOD = """\
tariffs:
  grid:
    type: flat_rate
    import_price_eur_per_kwh: 0.30
    default: true

devices:
  - id: baseline
    category: consumer
    type: generic_consumer
"""

SCHEMA_ERRORS = """\
tariffs:
  grid:
    type: flat_rate
    import_price_eur_per_kwh: 0.30
    default: true

devices:
  - id: baseline
    category: consumer
    type: generic_consumer
  - id: heater
    category: heater_thing
    type: generic_consumer
  - id: fridge
    category: consumer
    type: generic_consumer
    params:
      nonsense: 1
"""

INVALID_YAML = """\
devices:
  - id: baseline
    category: consumer
   type: generic_consumer
"""

MISSING_SECRET = GOOD + """\
  - id: grid_meter
    category: meter
    type: tasmota
    host: 127.0.0.1:9
    password: !secret meter_password
"""

UNREACHABLE = GOOD + """\
  - id: grid_meter
    category: meter
    type: tasmota
    host: 127.0.0.1:9          # nothing listens here
    profile: iskra_mt175
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


Starter = Callable[[str | None], Awaitable[tuple[Application, httpx.AsyncClient, Path]]]


@pytest.fixture
async def start(tmp_path: Path, monkeypatch) -> AsyncIterator[Starter]:
    """Start an app on a config text (``None``: no config file at all)."""
    monkeypatch.setenv("NORNWATT_PORT", str(_free_port()))
    monkeypatch.setenv("NORNWATT_DRY_RUN", "1")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    started: list[tuple[Application, httpx.AsyncClient]] = []

    async def _start(text: str | None) -> tuple[Application, httpx.AsyncClient, Path]:
        config = tmp_path / "config.yaml"
        if text is not None:
            config.write_text(text)
        app = Application(config_path=config, db_path=tmp_path / "data" / "history.db")
        await app.start()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app._api, client=("127.0.0.1", 1)),
            base_url="http://test",
        )
        started.append((app, client))
        return app, client, config

    yield _start
    for app, client in started:
        await client.aclose()
        await app.stop()


def _runtime_running(app: Application) -> bool:
    names = {t.get_name() for t in app.tasks if not t.done()}
    return {"polling", "planning", "control"} <= names


async def _configuration(client: httpx.AsyncClient) -> dict:
    resp = await client.get("/api/status")
    assert resp.status_code == 200, resp.text
    return resp.json()["configuration"]


# ── starting ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [None, "", "# nothing yet\n"], ids=["no file", "empty", "comments only"])
async def test_starts_without_a_configuration(start, text) -> None:
    app, client, _ = await start(text)

    configuration = await _configuration(client)
    assert configuration["state"] == "running"
    assert configuration["devices"] == 0
    assert configuration["problems"] == []
    assert (await client.get("/api/config/validate")).json()["ok"] is True
    listing = (await client.get("/api/config/file")).json()
    assert listing["devices"] == [] and listing["configuration"]["devices"] == 0
    assert _runtime_running(app)


async def test_invalid_yaml_starts_waiting_for_a_configuration(start) -> None:
    app, client, _ = await start(INVALID_YAML)

    configuration = await _configuration(client)
    assert configuration["state"] == "configuration_needed"
    [problem] = configuration["problems"]
    assert problem.startswith("config.yaml is not valid YAML: line 4, column 4:")
    assert (await client.get("/health")).json() == {"status": "ok", "configuration": "configuration_needed"}
    # Nothing is planned or controlled, but the API and the services run.
    assert not _runtime_running(app)
    assert {t.get_name() for t in app.tasks} == {"api", "retention"}
    # The text editor still opens the file.
    text = (await client.get("/api/config/text")).json()
    assert text["text"] == INVALID_YAML
    assert text["errors"] == configuration["problems"]


async def test_schema_errors_are_all_listed(start) -> None:
    app, client, _ = await start(SCHEMA_ERRORS)

    configuration = await _configuration(client)
    assert configuration["state"] == "configuration_needed"
    problems = configuration["problems"]
    assert len(problems) == 2
    assert any("heater" in p and "heater_thing" in p for p in problems)
    assert any("fridge" in p and "nonsense" in p for p in problems)
    validate = (await client.get("/api/config/validate")).json()
    assert validate["ok"] is False and validate["errors"] == problems
    # The valid parts can be read (and edited) in the forms.
    listing = (await client.get("/api/config/file")).json()
    assert [d["id"] for d in listing["devices"]] == ["baseline", "heater", "fridge"]
    assert not _runtime_running(app)


async def test_missing_secret_is_listed_and_set_from_the_ui(start) -> None:
    app, client, _ = await start(MISSING_SECRET)

    configuration = await _configuration(client)
    assert configuration["problems"] == ["secret 'meter_password' is not defined in secrets.yaml"]
    secrets = (await client.get("/api/config/file")).json()["secrets"]
    assert secrets == [{"name": "meter_password", "used_by": ["devices.grid_meter.password"],
                        "from_env": False, "defined": False}]

    resp = await client.put("/api/config/secrets/meter_password", json={"value": "hunter2"})

    assert resp.status_code == 200, resp.text
    assert (await _configuration(client))["state"] == "running"
    assert _runtime_running(app)


async def test_every_missing_secret_is_listed(start) -> None:
    _, client, _ = await start(MISSING_SECRET.replace(
        "host: 127.0.0.1:9", "host: !secret meter_host"))

    problems = (await _configuration(client))["problems"]
    assert problems == [
        "secret 'meter_host' is not defined in secrets.yaml",
        "secret 'meter_password' is not defined in secrets.yaml",
    ]


async def test_unreachable_device_starts_and_shows_unavailable(start) -> None:
    app, client, _ = await start(UNREACHABLE)

    assert (await _configuration(client))["state"] == "running"
    assert _runtime_running(app)
    await asyncio.wait_for(app._first_poll_done.wait(), 15)
    devices = {d["device_id"]: d for d in (await client.get("/api/status")).json()["devices"]}
    assert devices["grid_meter"]["available"] is False


async def test_unknown_plugin_type_is_listed(start) -> None:
    _, client, _ = await start(GOOD + "  - id: wallbox\n    category: charger\n    type: no_such_plugin\n")

    configuration = await _configuration(client)
    assert configuration["state"] == "configuration_needed"
    assert any("no_such_plugin" in p for p in configuration["problems"])


async def test_a_build_failure_starts_waiting_for_a_configuration(start, monkeypatch) -> None:
    from nornwatt import server

    real_build = server.build

    def failing_build(cfg, ctx=None):
        if cfg.devices:
            raise RuntimeError("plugin exploded while building")
        return real_build(cfg, ctx=ctx)

    monkeypatch.setattr(server, "build", failing_build)
    app, client, _ = await start(GOOD)

    configuration = await _configuration(client)
    assert configuration == {**configuration, "state": "configuration_needed",
                             "problems": ["plugin exploded while building"]}
    assert not _runtime_running(app)


async def test_run_forever_keeps_running_with_a_broken_config(tmp_path, monkeypatch) -> None:
    """The process itself: run_forever must not end on a config mistake."""
    monkeypatch.setenv("NORNWATT_PORT", str(_free_port()))
    monkeypatch.setenv("NORNWATT_DRY_RUN", "1")
    config = tmp_path / "config.yaml"
    config.write_text(INVALID_YAML)
    app = Application(config_path=config, db_path=tmp_path / "data" / "history.db")
    task = asyncio.create_task(app.run_forever())
    try:
        for _ in range(100):
            if app.tasks:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        assert not task.done(), task.exception() if task.done() else None
        assert app.config_problems
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ── fixing through the API ──────────────────────────────────────────────────


async def test_invalid_yaml_is_fixed_in_the_text_editor(start) -> None:
    app, client, config = await start(INVALID_YAML)
    revision = (await client.get("/api/config/text")).json()["revision"]

    # A step that still has problems is saved: nothing runs that it could break.
    half = INVALID_YAML.replace("   type:", "    type: [")
    resp = (await client.put("/api/config/text", json={"text": half, "revision": revision})).json()
    assert resp["ok"] is True and resp["applied"] is False
    assert resp["problems"][0].startswith("config.yaml is not valid YAML")
    assert config.read_text() == half

    resp = (await client.put("/api/config/text", json={"text": GOOD, "revision": resp["revision"]})).json()

    assert resp == {**resp, "ok": True, "applied": True, "problems": []}
    assert (await _configuration(client))["state"] == "running"
    assert _runtime_running(app)
    assert config.read_text() == GOOD


async def test_schema_errors_are_fixed_one_by_one_in_the_forms(start) -> None:
    app, client, config = await start(SCHEMA_ERRORS)
    listing = (await client.get("/api/config/file")).json()

    # A valid part is edited while the file still has other problems.
    baseline = next(d for d in listing["devices"] if d["id"] == "baseline")
    check = (await client.post("/api/config/validate", json={
        "kind": "devices", "id": "baseline", "value": {**baseline, "label": "The house"},
    })).json()
    assert check["ok"] is True and check["errors"] == []
    assert len(check["other_problems"]) == 2
    resp = await client.put("/api/config/devices/baseline", json={
        "value": {**baseline, "label": "The house"}, "revision": listing["revision"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False and len(resp.json()["problems"]) == 2
    assert "label: The house" in config.read_text()

    # A change that adds a problem is still refused.
    listing = (await client.get("/api/config/file")).json()
    resp = await client.put("/api/config/tariffs/spot", json={
        "value": {"type": "no_such_tariff", "settings": {}}, "create": True,
        "revision": listing["revision"],
    })
    assert resp.status_code == 422
    assert all("no_such_tariff" in e for e in resp.json()["detail"]["errors"])

    # Fix the heater, then remove the fridge: running.
    heater = next(d for d in listing["devices"] if d["id"] == "heater")
    resp = await client.put("/api/config/devices/heater", json={
        "value": {**heater, "category": "consumer"}, "revision": listing["revision"],
    })
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["problems"]) == 1
    resp = await client.delete("/api/config/devices/fridge",
                               params={"revision": resp.json()["revision"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True

    assert (await _configuration(client)) == {
        **(await _configuration(client)), "state": "running", "problems": [], "devices": 2,
    }
    assert _runtime_running(app)


async def test_hand_fixed_file_is_applied_with_reload(start) -> None:
    app, client, config = await start(SCHEMA_ERRORS)
    config.write_text(INVALID_YAML)

    # Applying a still-broken file shows its current problems.
    resp = await client.post("/api/config/reload")
    assert resp.status_code == 422
    assert (await _configuration(client))["problems"][0].startswith("config.yaml is not valid YAML")

    config.write_text(GOOD)
    resp = await client.post("/api/config/reload")

    assert resp.status_code == 200, resp.text
    assert (await _configuration(client))["state"] == "running"
    assert _runtime_running(app)


# ── a running configuration is never broken from the UI ─────────────────────


async def test_running_config_is_never_replaced_by_a_broken_one(start) -> None:
    app, client, config = await start(GOOD)
    revision = (await client.get("/api/config/text")).json()["revision"]

    for text in (INVALID_YAML, SCHEMA_ERRORS, MISSING_SECRET):
        resp = await client.put("/api/config/text", json={"text": text, "revision": revision})
        assert resp.status_code == 422, text
        assert resp.json()["detail"]["errors"]

    listing = (await client.get("/api/config/file")).json()
    resp = await client.put("/api/config/devices/heater", json={
        "value": {"id": "heater", "category": "heater_thing", "type": "generic_consumer"},
        "create": True, "revision": listing["revision"],
    })
    assert resp.status_code == 422
    resp = await client.put("/api/config/secrets/unused", json={"value": "x"})
    assert resp.status_code == 200   # unused secrets change nothing that runs

    assert config.read_text() == GOOD
    assert (await _configuration(client))["state"] == "running"
    assert _runtime_running(app)
    await asyncio.sleep(0.2)
    assert all(not t.done() for t in app.tasks)


# ── Home Assistant of the add-on (Supervisor) ───────────────────────────────


def test_addon_connects_to_its_home_assistant(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    cfg = YamlConfigLoader(tmp_path / "config.yaml").load_text("")
    ha = cfg.backends.homeassistant

    assert ha is not None and ha.supervisor
    assert (ha.url, ha.ws_url, ha.token) == (SUPERVISOR_CORE_URL, SUPERVISOR_CORE_WS_URL,
                                             "supervisor-token")


def test_configured_home_assistant_wins_over_the_supervisor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    cfg = YamlConfigLoader(tmp_path / "config.yaml").load_text(
        "backends:\n  homeassistant:\n    url: http://ha.remote:8123\n    token: abc\n")
    ha = cfg.backends.homeassistant

    assert ha is not None and not ha.supervisor
    assert (ha.url, ha.ws_url) == ("http://ha.remote:8123", None)


def test_no_home_assistant_outside_the_addon(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert YamlConfigLoader(tmp_path / "config.yaml").load_text("").backends.homeassistant is None


async def test_ha_client_uses_the_supervisor_websocket(monkeypatch) -> None:
    from nornwatt.loader.device_loader import make_build_context

    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    ctx = make_build_context(fallback_config(""))
    try:
        assert ctx.ha_client is not None
        assert ctx.ha_client._ws_url() == SUPERVISOR_CORE_WS_URL
        assert str(ctx.ha_client._client.base_url) == SUPERVISOR_CORE_URL + "/"
    finally:
        await ctx.ha_client.close()


async def test_configuration_state_names_the_addons_home_assistant(start, monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    _, client, _ = await start(None)

    assert (await _configuration(client))["homeassistant"] == {
        "source": "supervisor", "label": "Home Assistant (this installation)",
    }


# ── the fallback configuration ──────────────────────────────────────────────


def test_fallback_keeps_what_keeps_the_ui_usable(monkeypatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    cfg = fallback_config(
        "server:\n  port: 9000\n  api_token: tok\n"
        "backends:\n  homeassistant: {url: 'http://ha:8123', token: t}\n  mqtt: {host: broker}\n"
        "devices:\n  - id: x\n    category: nope\n"
    )
    assert cfg.devices == {} and cfg.tariffs == {}
    assert cfg.server == {"port": 9000, "api_token": "tok"}
    assert cfg.backends.homeassistant is not None and cfg.backends.homeassistant.url == "http://ha:8123"
    # The MQTT publisher would remove the entities of the missing devices.
    assert cfg.backends.mqtt is None


def test_fallback_locks_the_api_when_its_token_cannot_be_read() -> None:
    cfg = fallback_config("server:\n  api_token: tok\n  port: [\n")
    assert cfg.server["api_token"] and cfg.server["api_token"] != "tok"
    cfg = fallback_config("server:\n  api_token: !secret missing\n")
    assert cfg.server["api_token"]
    assert fallback_config("devices: [\n").server == {}


async def test_broken_config_with_api_token_refuses_remote_requests(start) -> None:
    app, _, _ = await start("server:\n  api_token: !secret missing_token\n")
    assert app.config_problems
    remote = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app._api, client=("192.168.1.50", 1)),
        base_url="http://test",
    )
    async with remote:
        assert (await remote.get("/api/status")).status_code == 401
        assert (await remote.get("/health")).status_code == 200

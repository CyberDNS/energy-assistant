"""Configuration editor API: edit config.yaml from the UI and apply it live."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from energy_assistant.server import Application

CONFIG = """\
# My house
tariffs:
  grid:
    type: flat_rate
    import_price_eur_per_kwh: 0.30   # all day
    default: true

devices:
  # ── Metering ──
  - id: grid_meter
    category: meter
    type: tasmota
    host: 127.0.0.1:9          # nothing listens here
    profile: iskra_mt175
    password: !secret meter_password
    params:
      max_import_kw: 24

  # ── Consumers ──
  - id: baseline
    category: consumer
    label: Baseline   # the rest of the house
    type: generic_consumer
    metered_by: grid_meter

optimizer:
  horizon_hours: 4
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def running(tmp_path: Path, monkeypatch) -> AsyncIterator[tuple[Application, httpx.AsyncClient, Path]]:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    (tmp_path / "secrets.yaml").write_text("meter_password: hunter2\n")
    monkeypatch.setenv("ENERGY_ASSISTANT_PORT", str(_free_port()))
    monkeypatch.setenv("ENERGY_ASSISTANT_DRY_RUN", "1")
    app = Application(config_path=config, db_path=tmp_path / "data" / "history.db")
    await app.start()
    transport = httpx.ASGITransport(app=app._api, client=("127.0.0.1", 1))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client, config
    finally:
        await app.stop()


def _device(listing: dict, device_id: str) -> dict:
    return next(d for d in listing["devices"] if d["id"] == device_id)


async def test_schema_describes_categories_and_plugins(running) -> None:
    _app, client, _config = running
    schema = (await client.get("/api/config/schema")).json()

    charger = next(c for c in schema["categories"] if c["category"] == "charger")
    assert charger["required_capabilities"] == ["plugged", "power_w"]
    assert charger["params"]["default_vehicle"]["ref"] == ["vehicle"]
    assert charger["params"]["guest_mode"]["choices"] == ["pv_only", "instant", "off"]
    assert charger["connections"] == {"metered_by": ["meter"]}
    assert charger["title"] == "Charger"
    assert charger["params"]["guest_mode"]["default"] == "pv_only"
    assert charger["params"]["max_power_kw"]["label"] == "Highest charging power (kW)"
    assert [c["category"] for c in schema["categories"]][:2] == ["meter", "pv"]
    meter = schema["categories"][0]
    assert meter["params"]["max_import_kw"]["when"] == {"metered_by": None}

    tasmota = next(p for p in schema["plugins"] if p["type"] == "tasmota")
    assert tasmota["title"] == "Tasmota (meter reader or plug)"
    assert tasmota["settings"]["host"]["required"] is True
    assert tasmota["settings"]["host"]["label"] == "Host"
    assert tasmota["settings"]["sensor"]["advanced"] is True
    zendure = next(p for p in schema["plugins"] if p["type"] == "zendure")
    assert zendure["settings"]["cloud_key"]["when"] == {"connection": "cloud"}
    assert {"capability": "soc_pct", "title": "State of charge"} in schema["capabilities"]
    assert [p["type"] for p in schema["plugins"] if "tibber" in p["type"]] \
        == ["tibber_cloud", "tibber_local"]
    assert tasmota["settings"]["password"]["secret"] is True
    vehicle = next(p for p in schema["plugins"] if p["type"] == "homeassistant_vehicle")
    assert "soc_pct" in vehicle["bindable"]
    assert vehicle["binding"]["entity"]["entity"] is True


async def test_devices_are_listed_without_secret_values(running) -> None:
    _app, client, _config = running
    listing = (await client.get("/api/config/file")).json()

    assert [d["id"] for d in listing["devices"]] == ["grid_meter", "baseline"]
    meter = _device(listing, "grid_meter")
    assert meter["settings"] == {"host": "127.0.0.1:9", "profile": "iskra_mt175", "password": {"!secret": "meter_password"}}
    assert meter["params"] == {"max_import_kw": 24}
    assert _device(listing, "baseline")["metered_by"] == "grid_meter"
    assert listing["tariffs"] == [{
        "id": "grid", "type": "flat_rate",
        "settings": {"import_price_eur_per_kwh": 0.30, "default": True},
    }]
    assert listing["secrets"] == [{
        "name": "meter_password", "used_by": ["devices.grid_meter.password"],
        "from_env": False, "defined": True,
    }]
    assert listing["sections"]["optimizer"] == {"horizon_hours": 4}
    assert listing["pending_reload"] is False
    assert "hunter2" not in str(listing)


async def test_changing_a_device_rewrites_only_its_lines_and_applies_live(running) -> None:
    app, client, config = running
    listing = (await client.get("/api/config/file")).json()
    device = _device(listing, "baseline")
    device["label"] = "Everything else"

    resp = await client.put("/api/config/devices/baseline",
                            json={"value": device, "revision": listing["revision"]})

    assert resp.status_code == 200, resp.text
    # Only the changed line differs; comments and the other entries are untouched.
    assert config.read_text() == CONFIG.replace(
        "label: Baseline   # the rest", "label: Everything else # the rest"
    )
    # Applied without a restart: the running configuration has the new label.
    running_cfg = (await client.get("/api/config")).json()
    assert next(d for d in running_cfg["devices"] if d["device_id"] == "baseline")["label"] \
        == "Everything else"
    assert {t.get_name() for t in app.tasks if not t.done()} \
        == {"polling", "planning", "control", "failsafe", "api", "retention"}
    backups = list((config.parent / "config_backups").glob("config-*.yaml"))
    assert [b.read_text() for b in backups] == [CONFIG]


async def test_adding_and_removing_a_device(running) -> None:
    _app, client, config = running
    new = {
        "id": "sauna", "category": "consumer", "type": "generic_consumer",
        "label": "Sauna", "metered_by": "grid_meter", "settings": {}, "capabilities": {},
        "params": {},
    }

    resp = await client.put("/api/config/devices/sauna", json={"value": new, "create": True})
    assert resp.status_code == 200, resp.text
    assert "  - id: sauna\n    category: consumer\n    label: Sauna\n" in config.read_text()
    running_ids = [d["device_id"] for d in (await client.get("/api/config")).json()["devices"]]
    assert "sauna" in running_ids

    # The same id again is refused.
    again = await client.put("/api/config/devices/sauna", json={"value": new, "create": True})
    assert again.status_code == 422

    resp = await client.delete("/api/config/devices/sauna")
    assert resp.status_code == 200, resp.text
    assert config.read_text() == CONFIG
    running_ids = [d["device_id"] for d in (await client.get("/api/config")).json()["devices"]]
    assert "sauna" not in running_ids


async def test_invalid_change_is_reported_and_nothing_is_written(running) -> None:
    _app, client, config = running
    listing = (await client.get("/api/config/file")).json()
    device = _device(listing, "baseline")
    device["metered_by"] = "no_such_meter"
    device["settings"] = {"hots": "x"}

    check = (await client.post(
        "/api/config/validate", json={"kind": "devices", "id": "baseline", "value": device}
    )).json()
    resp = await client.put("/api/config/devices/baseline", json={"value": device})

    assert check["ok"] is False
    assert any("no_such_meter" in e for e in check["errors"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"] == check["errors"]
    assert config.read_text() == CONFIG

    # Removing a device others depend on is a validation error too.
    resp = await client.delete("/api/config/devices/grid_meter")
    assert resp.status_code == 422
    assert any("grid_meter" in e for e in resp.json()["detail"]["errors"])
    assert config.read_text() == CONFIG


async def test_plugin_settings_are_validated(running) -> None:
    _app, client, _config = running
    device = {
        "id": "pv_meter", "category": "meter", "type": "tasmota", "metered_by": "grid_meter",
        "settings": {"poll_interval_s": "fast", "hots": "10.0.0.5"},
    }

    check = (await client.post("/api/config/validate", json={
        "kind": "devices", "id": "pv_meter", "value": device, "create": True,
    })).json()

    assert check["errors"] == [
        "device 'pv_meter': poll_interval_s must be a float, got 'fast'",
        ("device 'pv_meter': unknown setting 'hots' of plugin 'tasmota' (allowed: host, profile, "
         "sensor, fields, invert_sign, user, password, poll_interval_s, stale_after_s)"),
        ("device 'pv_meter': setting 'host' is required by plugin 'tasmota' "
         "(Host name, IP address or URL)"),
    ]


async def test_stale_revision_is_a_conflict(running) -> None:
    _app, client, config = running
    listing = (await client.get("/api/config/file")).json()
    config.write_text(CONFIG + "\n# edited by hand\n")

    assert (await client.get("/api/config/file")).json()["pending_reload"] is True
    device = _device(listing, "baseline")
    device["label"] = "X"
    resp = await client.put("/api/config/devices/baseline",
                            json={"value": device, "revision": listing["revision"]})

    assert resp.status_code == 409
    assert config.read_text() == CONFIG + "\n# edited by hand\n"


async def test_reload_applies_a_hand_edited_file_and_rejects_a_broken_one(running) -> None:
    app, client, config = running
    config.write_text(CONFIG.replace("label: Baseline ", "label: By hand "))

    resp = await client.post("/api/config/reload")

    assert resp.status_code == 200, resp.text
    assert app.running_devices()["baseline"].label == "By hand"
    assert (await client.get("/api/config/file")).json()["pending_reload"] is False

    config.write_text(CONFIG.replace("category: consumer", "category: toaster"))
    resp = await client.post("/api/config/reload")
    assert resp.status_code == 422
    assert "unknown category 'toaster'" in resp.json()["detail"]["errors"][0]
    # The previous configuration keeps running.
    assert app.running_devices()["baseline"].label == "By hand"
    assert all(not t.done() for t in app.tasks)


async def test_run_forever_survives_a_reload(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    (tmp_path / "secrets.yaml").write_text("meter_password: hunter2\n")
    monkeypatch.setenv("ENERGY_ASSISTANT_PORT", str(_free_port()))
    monkeypatch.setenv("ENERGY_ASSISTANT_DRY_RUN", "1")
    app = Application(config_path=config, db_path=tmp_path / "history.db")
    main = asyncio.create_task(app.run_forever())
    try:
        async with asyncio.timeout(20):
            while not app.tasks:
                await asyncio.sleep(0.05)
        await app.reload_config()
        await asyncio.sleep(0.2)
        assert not main.done()
        assert all(not t.done() for t in app.tasks)
    finally:
        main.cancel()
        await asyncio.gather(main, return_exceptions=True)


async def test_settings_section_is_changed_in_place(running) -> None:
    app, client, config = running
    listing = (await client.get("/api/config/file")).json()

    resp = await client.put("/api/config/sections/optimizer", json={
        "value": {"horizon_hours": 6, "step_minutes": 15}, "revision": listing["revision"],
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["restart_required"] == []
    assert config.read_text() == CONFIG.replace(
        "  horizon_hours: 4\n", "  horizon_hours: 6\n  step_minutes: 15\n"
    )
    assert app._horizon.total_seconds() == 6 * 3600

    # A section that is not in the file yet is added; restart-only settings are named.
    resp = await client.put("/api/config/sections/server", json={"value": {"port": 9000}})
    assert resp.json()["restart_required"] == ["port"]
    assert config.read_text().endswith("\nserver:\n  port: 9000\n")

    bad = await client.put("/api/config/sections/optimizer", json={"value": {"horizon": 6}})
    assert bad.status_code == 422
    assert bad.json()["detail"]["errors"] == [
        ("optimizer: unknown setting 'horizon' (allowed: horizon_hours, step_minutes, precision, "
         "algorithm)")
    ]


async def test_tariffs_are_added_checked_and_removed(running) -> None:
    app, client, config = running

    resp = await client.put("/api/config/tariffs/night", json={
        "value": {"type": "flat_rate", "settings": {"import_price_eur_per_kwh": 0.2}},
        "create": True,
    })
    assert resp.status_code == 200, resp.text
    assert "  night:\n    type: flat_rate\n    import_price_eur_per_kwh: 0.2\n" in config.read_text()
    assert "night" in app._tariffs

    check = (await client.post("/api/config/validate", json={
        "kind": "tariffs", "id": "night",
        "value": {"type": "tibber", "settings": {"resolution": "DAILY"}},
    })).json()
    assert check["errors"] == [
        "tariffs: night: resolution must be one of QUARTER_HOURLY, HOURLY, got 'DAILY'",
        "tariffs: night: setting 'token' is required (Personal access token from developer.tibber.com)",
    ]

    # A tariff a device uses cannot go.
    listing = (await client.get("/api/config/file")).json()
    device = _device(listing, "baseline")
    device["tariff"] = "night"
    assert (await client.put("/api/config/devices/baseline", json={"value": device})).status_code == 200
    resp = await client.delete("/api/config/tariffs/night")
    assert resp.status_code == 422
    assert "tariff 'night' is not a configured tariff" in resp.json()["detail"]["errors"][0]


async def test_secrets_are_set_in_secrets_yaml_and_never_returned(running) -> None:
    _app, client, config = running
    secrets = config.parent / "secrets.yaml"

    resp = await client.put("/api/config/secrets/tibber_token", json={"value": "tok-123"})
    assert resp.status_code == 200, resp.text
    assert secrets.read_text() == "meter_password: hunter2\ntibber_token: tok-123\n"
    assert secrets.stat().st_mode & 0o777 == 0o600

    # Use it, by name, in a new tariff.
    resp = await client.put("/api/config/tariffs/spot", json={
        "value": {"type": "tibber", "settings": {"token": {"!secret": "tibber_token"}}},
        "create": True,
    })
    assert resp.status_code == 200, resp.text
    assert "    token: !secret tibber_token\n" in config.read_text()

    listing = (await client.get("/api/config/file")).json()
    assert "tok-123" not in str(listing) and "hunter2" not in str(listing)
    assert next(s for s in listing["secrets"] if s["name"] == "tibber_token")["used_by"] \
        == ["tariffs.spot.token"]

    # Replacing a value that is in use applies the configuration again.
    assert (await client.put("/api/config/secrets/tibber_token", json={"value": "tok-456"})).status_code == 200
    assert "tibber_token: tok-456" in secrets.read_text()

    in_use = await client.delete("/api/config/secrets/tibber_token")
    assert in_use.status_code == 409
    assert "still used by tariffs.spot.token" in in_use.json()["detail"]["errors"][0]

    assert (await client.put("/api/config/secrets/spare", json={"value": "x"})).status_code == 200
    assert (await client.delete("/api/config/secrets/spare")).status_code == 200
    assert "spare" not in secrets.read_text()
    assert (await client.put("/api/config/secrets/bad name", json={"value": "x"})).status_code == 422


async def test_plain_secret_in_a_section_is_hidden_and_kept(running) -> None:
    _app, client, config = running
    config.write_text(CONFIG + "\nserver:\n  api_token: plain-token\n")
    assert (await client.post("/api/config/reload")).status_code == 200

    listing = (await client.get("/api/config/file")).json()
    assert listing["sections"]["server"] == {"api_token": {"!hidden": True}}

    resp = await client.put("/api/config/sections/server", json={
        "value": {"api_token": {"!hidden": True}, "history_retention_days": 5},
    })
    assert resp.status_code == 200, resp.text
    assert config.read_text().endswith(
        "server:\n  api_token: plain-token\n  history_retention_days: 5\n"
    )


async def test_unknown_secret_is_a_validation_error(running) -> None:
    _app, client, _config = running
    check = (await client.post("/api/config/validate", json={
        "kind": "tariffs", "id": "spot", "create": True,
        "value": {"type": "tibber", "settings": {"token": {"!secret": "nope"}}},
    })).json()
    assert check["errors"] == ["secret 'nope' is not defined in secrets.yaml"]


async def test_state_reports_a_file_that_waits_to_be_applied(running) -> None:
    _, client, config = running
    state = (await client.get("/api/config/state")).json()
    assert state["pending_reload"] is False
    assert state["revision"] == (await client.get("/api/config/file")).json()["revision"]

    config.write_text(CONFIG.replace("label: Baseline ", "label: By hand "))
    assert (await client.get("/api/config/state")).json()["pending_reload"] is True

    await client.post("/api/config/reload")
    assert (await client.get("/api/config/state")).json()["pending_reload"] is False

"""Browser test: a fresh install, set up entirely in the web UI.

The real Energy Assistant (not a mocked API) starts with no config.yaml at
all, next to a stand-in for Home Assistant's REST API.  In Chromium:

1. Home shows the first-start page (no devices yet), no error;
2. Settings → Connections: the Home Assistant URL is typed and its token
   created as a new secret; saved and applied without a restart;
3. Settings → Devices: a grid meter reading a Home Assistant sensor is
   added, picking the entity from the list Home Assistant offers;
4. Home shows the normal dashboard, the meter's reading arrives and a plan
   made with the meter appears.

Needs the built frontend, Playwright and its Chromium; skips itself without
them.  See ``test_savings_ranges.py`` for how to run it locally.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from energy_assistant.server import Application

FRONTEND = Path(__file__).resolve().parents[2] / "src" / "energy_assistant" / "frontend"

STATES = [
    {
        "entity_id": "sensor.grid_power", "state": "420",
        "attributes": {"unit_of_measurement": "W", "friendly_name": "Grid power", "device_class": "power"},
        "last_changed": "2026-11-24T12:00:00+00:00", "last_updated": "2026-11-24T12:00:00+00:00",
    },
]


class _FakeHomeAssistant(http.server.BaseHTTPRequestHandler):
    """The REST API of Home Assistant, as far as the app reads it.  No
    websocket: the app falls back to REST, as with a websocket outage."""

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.headers.get("Authorization") != "Bearer ha-test-token":
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/api/states":
            body: object = STATES
        elif self.path.startswith("/api/states/"):
            match = [s for s in STATES if s["entity_id"] == self.path.rsplit("/", 1)[1]]
            if not match:
                self.send_response(404)
                self.end_headers()
                return
            body = match[0]
        elif self.path == "/api/":
            body = {"message": "API running."}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def fake_ha() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHomeAssistant)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@contextlib.contextmanager
def _app_in_thread(tmp_path: Path, monkeypatch) -> Iterator[str]:
    """The real app on a free port, on tmp_path/config.yaml (if any)."""
    if not (FRONTEND / "index.html").exists():
        pytest.skip("frontend not built (cd frontend && npm run build)")
    port = _free_port()
    monkeypatch.setenv("ENERGY_ASSISTANT_PORT", str(port))
    monkeypatch.setenv("ENERGY_ASSISTANT_DRY_RUN", "1")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("ENERGY_ASSISTANT_API_TOKEN", raising=False)
    app = Application(config_path=tmp_path / "config.yaml", db_path=tmp_path / "data" / "history.db")
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(app.start())
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(30), "the app did not start"
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while True:
        try:
            if httpx.get(f"{base}/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, "the API did not come up"
        time.sleep(0.1)
    try:
        yield base
    finally:
        asyncio.run_coroutine_threadsafe(app.stop(), loop).result(30)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)


@pytest.fixture
def fresh_app(tmp_path: Path, monkeypatch) -> Iterator[tuple[str, Path]]:
    """The real app, started with no config.yaml at all."""
    with _app_in_thread(tmp_path, monkeypatch) as base:
        yield base, tmp_path / "config.yaml"


@pytest.fixture
def page():
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                yield browser.new_page(viewport={"width": 1280, "height": 1400})
            finally:
                browser.close()
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Chromium for Playwright is not installed")
        raise


def _plan_created(base: str) -> datetime | None:
    created = httpx.get(f"{base}/api/plan").json().get("created_at")
    return datetime.fromisoformat(created) if created else None


def test_fresh_install_is_set_up_in_the_ui(fresh_app, fake_ha, page) -> None:
    base, config = fresh_app
    expect = sync_api.expect
    assert not config.exists()

    # 1 — the first-start page, no error.
    page.goto(base + "/")
    first_start = page.get_by_test_id("first-start")
    expect(first_start).to_be_visible()
    expect(first_start).to_contain_text("Add your first device")
    expect(page.get_by_test_id("ha-connection")).to_have_count(0)
    expect(page.locator('[data-notice="configuration-needed"]')).to_have_count(0)

    # 2 — connect Home Assistant.
    first_start.get_by_role("link", name="Connect").click()
    ha = page.get_by_role("group", name="Home Assistant")
    ha.get_by_role("textbox", name="URL").fill(fake_ha)
    ha.get_by_role("combobox", name="Access token").select_option("+new")
    ha.get_by_label("Secret name").fill("ha_token")
    ha.get_by_label("Secret value").fill("ha-test-token")
    ha.get_by_role("button", name="Save secret").click()
    expect(ha.get_by_role("combobox", name="Access token")).to_have_value("ha_token")
    # Creating the secret keeps what was typed in the form.
    expect(ha.get_by_role("textbox", name="URL")).to_have_value(fake_ha)
    page.get_by_role("button", name="Save and apply").click()
    expect(page.get_by_role("status")).to_contain_text("Connections saved and applied")
    assert "token: !secret ha_token" in config.read_text()
    assert "ha-test-token" not in config.read_text()

    # 3 — the first device: the grid meter, from a Home Assistant sensor.
    before = _plan_created(base)
    page.goto(base + "/#/")
    expect(page.get_by_test_id("ha-connection")).to_contain_text(f"Connected to {fake_ha}")
    page.get_by_test_id("add-first-device").click()
    page.get_by_role("button", name="+ Add device").click()
    page.get_by_label("Kind of device").select_option("meter")
    page.get_by_role("textbox", name="ID").fill("grid")
    page.get_by_label("Connected through").select_option("homeassistant_sensor")
    entity = page.get_by_role("combobox", name="Entity").first
    entity.fill("grid_power")
    # Offered by Home Assistant through the new connection.
    page.get_by_role("option", name="sensor.grid_power").first.click()
    page.get_by_role("button", name="Add device", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("grid added and applied", timeout=20_000)
    assert "entity: sensor.grid_power" in config.read_text()

    # 4 — running: the dashboard, the meter's reading and a plan made with it.
    # ("/" alone would reopen the last page: the app remembers it.)
    page.goto(base + "/#/")
    expect(page.get_by_test_id("today-figures")).to_be_visible()
    expect(page.get_by_test_id("first-start")).to_have_count(0)
    deadline = time.monotonic() + 30
    while True:
        status = httpx.get(f"{base}/api/status").json()
        grid = next((d for d in status["devices"] if d["device_id"] == "grid"), None)
        created = _plan_created(base)
        if grid and grid["power_w"] == 420.0 and created and (before is None or created > before):
            break
        assert time.monotonic() < deadline, (status, created)
        time.sleep(0.5)
    assert status["configuration"] == {
        "state": "running", "problems": [], "devices": 1,
        "homeassistant": {"source": "config", "label": fake_ha},
    }
    page.get_by_role("link", name="Plan", exact=True).click()
    expect(page.get_by_text("Plan created:")).to_be_visible(timeout=20_000)


def test_broken_config_shows_its_problems_and_is_fixed_as_text(tmp_path, monkeypatch, page) -> None:
    """A config.yaml with a YAML mistake: the app starts, the banner lists the
    problem, the settings open the text editor, fixing it starts planning."""
    (tmp_path / "config.yaml").write_text("devices:\n  - id: grid\n   category: meter\n")
    with _app_in_thread(tmp_path, monkeypatch) as base:
        expect = sync_api.expect
        page.goto(base + "/")
        banner = page.locator('[data-notice="configuration-needed"]')
        expect(banner).to_contain_text("Nothing is planned or controlled")
        expect(page.get_by_test_id("config-problems")).to_contain_text("line 3, column 4")

        banner.get_by_role("link", name="Fix in settings").click()
        editor = page.get_by_test_id("config-text")
        expect(editor).to_be_visible()
        expect(editor.get_by_role("button", name="Go to line 3")).to_be_visible()
        editor.get_by_role("textbox", name="config.yaml").fill(
            "devices:\n  - id: house\n    category: consumer\n    type: generic_consumer\n"
        )
        editor.get_by_role("button", name="Save and apply").click()
        expect(page.get_by_role("status")).to_contain_text("config.yaml saved and applied")
        expect(banner).to_have_count(0)
        status = httpx.get(f"{base}/api/status").json()
        assert status["configuration"]["state"] == "running"


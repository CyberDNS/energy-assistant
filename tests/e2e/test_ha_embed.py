"""Browser test: the app inside Home Assistant's app panel.

A stand-in for Home Assistant frames the real frontend and speaks the
postMessage protocol of HA's ``ha-panel-app`` (``home-assistant/…`` messages),
recording what the app asks for:

* kiosk mode is requested by default and can be switched off per device,
* the "Home Assistant" button opens HA's menu,
* a link into the app opens that page, and page changes are mirrored into
  HA's URL without adding history entries,
* an embedded view (``?embed=1``) never talks to Home Assistant.

Needs the built frontend, Playwright and its Chromium; skips itself without
them.  See ``test_savings_ranges.py`` for how to run it locally.
"""

from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

FRONTEND = Path(__file__).resolve().parents[2] / "src" / "energy_assistant" / "frontend"

STATUS = {
    "timestamp": "2026-11-22T12:00:00+00:00", "planning": False, "dry_run": False,
    "grid_power_w": 420.0, "current_price_eur_per_kwh": 0.31, "pv_opportunity_price_eur_per_kwh": 0.08,
    "devices": [
        {"device_id": "grid", "label": "Grid meter", "category": "meter", "power_w": 420.0, "available": True},
        {"device_id": "pv", "label": "PV", "category": "pv", "power_w": -1500.0, "available": True},
        {"device_id": "bat", "label": "Battery", "category": "battery", "power_w": 600.0, "soc_pct": 55.0,
         "available": True},
    ],
    "setpoints": [],
    "ledger": [{"device_id": "bat", "stored_energy_kwh": 5.5, "capacity_kwh": 10.0, "cost_basis_eur_per_kwh": 0.12}],
}

# What ha-panel-app does with the messages of the framed app (HA frontend,
# src/panels/app/ha-panel-app.ts), reduced to what the app can observe.
HOST = """<!doctype html><meta charset="utf-8"><title>fake Home Assistant</title>
<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%;display:block}</style>
<iframe id="app"></iframe>
<script>
  const PREFIX = '/app/energy_assistant'
  const params = new URLSearchParams(location.search)
  window.ha = { path: params.get('route') ?? '', kiosk: false, subscribed: false, menuToggles: 0, navigations: [] }
  const frame = document.getElementById('app')
  const send = () => frame.contentWindow.postMessage({
    type: 'home-assistant/properties', narrow: false, route: { prefix: PREFIX, path: ha.path },
    safeAreaInsets: { top: '0px', right: '0px', bottom: '0px', left: '0px' },
  }, '*')
  window.addEventListener('message', (event) => {
    if (event.source !== frame.contentWindow) return
    const { type, ...data } = event.data
    if (type === 'home-assistant/subscribe-properties') {
      ha.subscribed = true
      send()
      if (data.kioskMode) ha.kiosk = true
    } else if (type === 'home-assistant/unsubscribe-properties') {
      ha.subscribed = false
      ha.kiosk = false
    } else if (type === 'home-assistant/toggle-menu') {
      ha.menuToggles++
    } else if (type === 'home-assistant/navigate') {
      ha.navigations.push({ path: data.path, replace: !!data.options?.replace })
      ha.path = data.path.slice(PREFIX.length)
      if (ha.subscribed) send()
    }
  })
  // HA's own back/forward or a link inside HA.
  window.haGoTo = (path) => { ha.path = path; send() }
  frame.src = params.get('app') ?? '/'
</script>
"""


@pytest.fixture
def server():
    if not (FRONTEND / "index.html").exists():
        pytest.skip("frontend not built (npm run build in frontend/)")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/host.html":
                return self._send(HOST.encode(), "text/html")
            if url.path == "/api/stream":
                self.send_response(404)
                self.end_headers()
                return None
            if url.path.startswith("/api/"):
                body: object = {}
                if url.path == "/api/status":
                    body = STATUS
                elif url.path in ("/api/vehicles", "/api/chargers", "/api/ledger"):
                    body = []
                return self._send(json.dumps(body).encode(), "application/json")
            if "." not in url.path.rsplit("/", 1)[-1]:
                self.path = "/index.html"
            return super().do_GET()

    Handler.extensions_map = {**Handler.extensions_map, ".js": "application/javascript"}
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(FRONTEND))
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def page():
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — no browser installed
            pytest.skip(f"Chromium not available: {exc}")
        pg = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors
        yield pg
        browser.close()


def _ha(page, expression: str):
    return page.evaluate(f"window.ha.{expression}")


def _nav(scope, path: str):
    """The sidebar item of a page (the status bar links to pages as well)."""
    return scope.locator(f'[data-slot="sidebar"] a[href="#{path}"]')


def _title(app) -> str:
    return app.locator('[data-testid="page-title"]').inner_text()


def test_takes_over_the_screen_and_offers_the_way_back(server, page) -> None:
    page.goto(f"{server}/host.html")
    app = page.frame_locator("#app")
    button = app.locator('[data-testid="ha-menu"]')
    button.wait_for()
    assert _ha(page, "kiosk") is True

    button.click()
    page.wait_for_function("window.ha.menuToggles === 1")

    # Per device: switched off in Settings, HA gets its own sidebar back at once.
    _nav(app, "/settings").click()
    app.locator('[data-testid="settings-interface"]').click()
    app.locator('[data-testid="kiosk-switch"]').click()
    page.wait_for_function("window.ha.kiosk === false && window.ha.subscribed === true")

    # …and it stays off after a reload.
    page.reload()
    app.locator('[data-testid="ha-menu"]').wait_for()
    assert _ha(page, "kiosk") is False
    assert page.errors == []


def test_follows_and_mirrors_home_assistants_url(server, page) -> None:
    # A link into the app opens that page.
    page.goto(f"{server}/host.html?route=/plan")
    app = page.frame_locator("#app")
    app.locator('[data-testid="ha-menu"]').wait_for()
    page.wait_for_function("document.querySelector('#app').contentWindow.location.hash === '#/plan'")
    assert _ha(page, "navigations") == []

    # A page change in the app rewrites HA's URL, without a new history entry.
    _nav(app, "/batteries").click()
    page.wait_for_function("window.ha.path === '/batteries'")
    assert _ha(page, "navigations") == [{"path": "/app/energy_assistant/batteries", "replace": True}]

    # HA moving on its own (back, forward, a link) moves the app, and is not echoed.
    page.evaluate("window.haGoTo('/savings')")
    page.wait_for_function("document.querySelector('#app').contentWindow.location.hash === '#/savings'")
    assert _title(app) == "Savings"
    page.wait_for_timeout(300)
    assert len(_ha(page, "navigations")) == 1

    # Home is HA's bare app URL.
    _nav(app, "/").first.click()
    page.wait_for_function("window.ha.path === ''")
    assert page.errors == []


def test_without_home_assistant_there_is_no_button(server, page) -> None:
    page.goto(f"{server}/")
    page.locator('[data-testid="status-bar"]').wait_for()
    page.locator('[data-testid="status-battery"]').wait_for()
    assert page.locator('[data-testid="ha-menu"]').count() == 0
    assert _nav(page, "/batteries").count() == 1  # the house has a battery
    assert _nav(page, "/cars").count() == 0       # but no car or charger
    assert page.errors == []


def test_an_embedded_view_leaves_home_assistant_alone(server, page) -> None:
    page.goto(f"{server}/host.html?app=/%3Fview%3Dsavings%26embed%3D1")
    app = page.frame_locator("#app")
    app.locator("text=Saving").first.wait_for()
    assert app.locator('[data-testid="status-bar"]').count() == 0
    assert _ha(page, "subscribed") is False
    assert _ha(page, "navigations") == []


def test_old_links_still_open_the_right_page(server, page) -> None:
    for old, new in (
        ("/#/config/tariffs", "#/settings/tariffs"),
        ("/#/history", "#/devices"),
        ("/#/plan/banzert", "#/cars/banzert"),
        ("/#/plan?vehicle=banzert", "#/cars/banzert"),
        ("/?vehicle=banzert", "#/cars/banzert"),
        ("/?view=learning", "#/learning"),
    ):
        page.goto("about:blank")
        page.goto(f"{server}{old}")
        page.wait_for_function("hash => location.hash === hash", arg=new)

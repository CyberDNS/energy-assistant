"""Browser test: switching ranges on the Savings tab.

Serves the real frontend with a mocked ``/api/savings`` and clicks through all
ranges in Chromium.  Guards against the slow range switch (Plotly.react with
changed date-axis tick settings took 4–15 s even for a few points).

Needs Playwright and its Chromium, which CI does not install — the test skips
itself without them.  Locally::

    uv run --with playwright playwright install chromium   # once
    uv run --with playwright pytest tests/e2e -v
"""

from __future__ import annotations

import functools
import http.server
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
RANGES = ["24 h", "3 d", "7 d", "30 d", "90 d", "1 y"]
MAX_SWITCH_S = 2.0


def _totals(real_cost: float, unc_cost: float, hours: float) -> dict:
    return {
        "real": unc_cost - real_cost, "uncontrolled_cost": unc_cost, "real_cost": real_cost,
        "uncontrolled_bill": unc_cost, "real_bill": real_cost, "ev_saving": 0.0, "hours": hours,
    }


def _savings(hours: float, n_slots: int) -> dict:
    """A /api/savings response with *n_slots* recent 15-minute slots."""
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 15)
    slots = []
    for i in range(n_slots):
        start = now - timedelta(minutes=15 * (n_slots - i))
        slots.append({
            "slot_start": start.isoformat(), "hours": 0.25,
            "real_cost": 0.10, "uncontrolled_cost": 0.10 + 0.02 * ((i % 5) - 2),
            "saving": 0.02 * ((i % 5) - 2), "real_bill": 0.10, "uncontrolled_bill": 0.10,
            "ev_saving": 0.0, "real_battery_kwh": -0.1 if i % 2 else 0.0,
            "uncontrolled_battery_kwh": -0.2, "real_ev_kwh": 2.5 if i % 7 == 0 else 0.0,
            "uncontrolled_ev_kwh": 0.0, "real_stored_kwh": 3.0 - 0.01 * i,
            "uncontrolled_stored_kwh": max(1.0, 3.0 - 0.05 * i), "import_price": 0.45,
            "stored_detail": {"real": {"bat": 3.0}, "uncontrolled": {"bat": 2.0}},
        })
    bucket = "hour" if hours <= 72 else "day" if hours <= 24 * 31 else "week"
    size = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}[bucket]
    buckets, t = [], now - timedelta(hours=min(hours, n_slots / 4))
    while t < now:
        buckets.append({"start": t.isoformat(), **_totals(0.4, 0.45, size.total_seconds() / 3600)})
        t += size
    totals = _totals(0.4 * len(buckets), 0.45 * len(buckets), n_slots / 4)
    return {
        "bucket": bucket, "totals": totals, "buckets": buckets, "all_time": totals,
        "stored": {"real": {"bat": {"kwh": 3.0, "value_eur": 0.6}},
                   "uncontrolled": {"bat": {"kwh": 1.0, "value_eur": 0.1}}},
        "slots": slots if hours <= 72 else None,
    }


@pytest.fixture
def server(request):
    n_slots = request.param

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet
            pass

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                body = b"{}"
                if url.path == "/api/savings":
                    hours = float(parse_qs(url.query).get("hours", ["720"])[0])
                    body = json.dumps(_savings(hours, n_slots)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/ui/"):
                self.path = self.path[3:]
            elif "." not in url.path.rsplit("/", 1)[-1]:
                self.path = "/index.html"
            super().do_GET()

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
        pg = browser.new_page(viewport={"width": 1300, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors
        yield pg
        browser.close()


# 3 slots: right after a (re)start — the case that was slowest live.
# 96 × 3 slots: three full days.
@pytest.mark.parametrize("server", [3, 288], indirect=True, ids=["fresh", "three_days"])
def test_switching_ranges_is_fast_and_shows_the_right_panels(server, page) -> None:
    page.goto(f"{server}/#/savings")
    page.wait_for_selector("text=Saving per")

    for label in RANGES + ["24 h", "3 d"]:
        start = time.monotonic()
        page.click(f'button.range-btn:text-is("{label}")')
        page.wait_for_function(
            "label => document.querySelector('button.range-btn.active')?.textContent.trim() === label",
            arg=label,
        )
        short = label in ("24 h", "3 d")
        # Done when the view shows this range's data: per-slot title and
        # detail panel only for the short ranges, and every chart drawn.
        page.wait_for_function(
            """short => {
                const titles = [...document.querySelectorAll('.panel h2')].map(h => h.textContent);
                const saving = titles.find(t => t.startsWith('Saving per')) ?? '';
                const detail = titles.some(t => t.startsWith('Batteries and cars'));
                const drawn = [...document.querySelectorAll('.panel .js-plotly-plot')]
                    .every(p => p.querySelector('.main-svg'));
                return saving.includes('15 min') === short && detail === short && drawn;
            }""",
            arg=short,
            timeout=30_000,
        )
        elapsed = time.monotonic() - start
        assert elapsed < MAX_SWITCH_S, f"switching to {label} took {elapsed:.1f} s"

    assert page.errors == []

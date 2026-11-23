"""The web UI is served from the built frontend inside the package."""

from __future__ import annotations

import re
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

import energy_assistant
from energy_assistant.server import Application

# Where `npm run build` puts the UI (frontend/vite.config.ts).
BUILT = Path(energy_assistant.__file__).resolve().parent / "frontend"

CONFIG = """\
tariffs:
  grid:
    type: flat_rate
    import_price_eur_per_kwh: 0.30
    default: true

devices:
  - id: grid_meter
    category: meter
    type: tasmota
    host: 127.0.0.1:9          # nothing listens here
    profile: iskra_mt175
  - id: baseline
    category: consumer
    type: generic_consumer
    metered_by: grid_meter

optimizer:
  horizon_hours: 4
"""


@pytest.fixture
async def client(tmp_path: Path, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setenv("ENERGY_ASSISTANT_PORT", str(s.getsockname()[1]))
    monkeypatch.setenv("ENERGY_ASSISTANT_DRY_RUN", "1")
    app = Application(config_path=config, db_path=tmp_path / "data" / "history.db")
    await app.start()
    transport = httpx.ASGITransport(app=app._api, client=("127.0.0.1", 1))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await app.stop()


async def test_root_serves_the_ui_or_says_how_to_build_it(client) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    if not (BUILT / "index.html").exists():
        assert "npm run build" in resp.text
        return

    assert '<div id="app">' in resp.text
    assert resp.headers["cache-control"] == "no-store"
    # Relative URLs only: behind Home Assistant ingress the app lives under a path prefix.
    urls = re.findall(r'(?:src|href)="([^"]+)"', resp.text)
    assert urls and all(u.startswith("./") for u in urls), urls

    script = next(u for u in urls if u.endswith(".js"))
    asset = await client.get(script[1:])
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    assert (await client.get("/favicon.svg")).status_code == 200
    # The API is not shadowed by the static files.
    assert (await client.get("/api/status")).headers["content-type"].startswith("application/json")

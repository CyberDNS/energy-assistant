"""Browser test: observe mode (#134) — the Home banner while every device is
only observed, and switching control on for one device from its page.

Needs the built frontend, Playwright and its Chromium; skips itself without
them.  See ``test_savings_ranges.py`` for how to run it locally.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

# The app-in-a-thread and browser fixtures of the first-start test.
from test_first_start import _app_in_thread, page  # noqa: F401

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
    host: 127.0.0.1:9
    profile: iskra_mt175
  - id: home_battery
    category: battery
    label: Home battery
    type: modbus
    profile: sma_sunny_boy_storage
    host: 127.0.0.1:9
    metered_by: grid_meter
    params:
      capacity_kwh: 7.4
      max_charge_kw: 3.0
      max_discharge_kw: 3.0
    control: observe
"""


@pytest.fixture
def observing_app(tmp_path: Path, monkeypatch) -> Iterator[tuple[str, Path]]:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    with _app_in_thread(tmp_path, monkeypatch) as base:
        yield base, config


def test_control_is_switched_on_per_device(observing_app, page) -> None:  # noqa: F811
    base, config = observing_app
    expect = sync_api.expect

    page.goto(base + "/")
    expect(page.get_by_test_id("observing-banner")).to_contain_text("Nornwatt is observing")

    page.goto(base + "/#/devices/home_battery")
    control = page.get_by_test_id("device-control")
    expect(control.get_by_test_id("control-chip")).to_have_text("Observing")
    control.get_by_test_id("control-switch").click()
    expect(control.get_by_test_id("control-confirm")).to_contain_text("takes over this device")
    control.get_by_test_id("control-confirm-ok").click()

    expect(control.get_by_test_id("control-chip")).to_have_text("Controlling")
    assert "    control: active\n" in config.read_text()

    page.goto(base + "/")
    expect(page.get_by_test_id("observing-banner")).to_have_count(0)

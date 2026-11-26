"""Browser test: one signed entity or separate import/export entities (#152).

The device form shows a switch for a power binding and only the fields of
the selected variant.  Needs the built frontend, Playwright and its Chromium;
skips itself without them.  See ``test_savings_ranges.py`` for how to run it.
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
  - id: grid
    category: meter
    label: Grid
    type: homeassistant_sensor
    capabilities:
      power_w: { import_entity: sensor.grid_import, export_entity: sensor.grid_export }
"""


@pytest.fixture
def split_app(tmp_path: Path, monkeypatch) -> Iterator[tuple[str, Path]]:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    with _app_in_thread(tmp_path, monkeypatch) as base:
        yield base, config


def test_power_binding_switches_between_one_and_two_entities(split_app, page) -> None:  # noqa: F811
    base, _config = split_app
    expect = sync_api.expect

    page.goto(base + "/#/settings/devices")
    page.get_by_role("button", name="Grid").first.click()
    power = page.locator(".cfg-cap").filter(has_text="power_w").first
    switch = power.get_by_role("radiogroup", name="Read from")
    single = switch.get_by_role("radio", name="One entity")
    split = switch.get_by_role("radio", name="Separate import and export")

    # The file has import + export: that variant is selected, and only its fields show.
    expect(split).to_have_attribute("aria-checked", "true")
    expect(power.get_by_role("group", name="Import power entity").get_by_role("combobox")
           ).to_have_value("sensor.grid_import")
    expect(power.get_by_role("group", name="Export power entity").get_by_role("combobox")
           ).to_have_value("sensor.grid_export")
    expect(power.get_by_role("group", name="Entity", exact=True)).to_have_count(0)
    expect(power.get_by_text("Invert sign")).to_have_count(0)

    # One entity: the import/export fields (and their values) go, entity comes.
    single.click()
    expect(single).to_have_attribute("aria-checked", "true")
    expect(power.get_by_role("group", name="Entity", exact=True)).to_be_visible()
    expect(power.get_by_text("Invert sign")).to_be_visible()
    expect(power.get_by_role("group", name="Import power entity")).to_have_count(0)

    # Back to two entities with nothing filled in: the pick holds.
    split.click()
    expect(split).to_have_attribute("aria-checked", "true")
    expect(power.get_by_role("group", name="Import power entity")).to_be_visible()

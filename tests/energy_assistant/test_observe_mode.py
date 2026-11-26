"""Observe mode (#134): devices are planned, but only controlled once the
user switches control on per device."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from energy_assistant.config.store import entry_to_form, form_to_entry
from energy_assistant.core.control import ControlLoop
from energy_assistant.core.device_model import DeviceConfigError, parse_devices
from energy_assistant.core.execution_log import DeviceTick, ExecutionLog
from energy_assistant.core.ledger import BatteryCostLedger
from energy_assistant.core.models import StorageConstraints, ThresholdConstraints
from energy_assistant.core.projection import ObservedProjection
from energy_assistant.core.savings import DeviceLive, EvLive, LiveSample, SavingsTracker

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)

BATTERY = {
    "category": "battery", "type": "zendure",
    "params": {"capacity_kwh": 10, "max_charge_kw": 2, "max_discharge_kw": 2},
}


# ── config: control: observe | active ────────────────────────────────────────


def test_control_defaults_to_active_and_observe_is_parsed() -> None:
    specs = parse_devices({
        "old": BATTERY,
        "new": {**BATTERY, "control": "observe"},
        "on": {**BATTERY, "control": "active"},
    })
    assert not specs["old"].observed and specs["old"].control is None
    assert specs["new"].observed
    assert not specs["on"].observed
    assert specs["new"].to_dict()["control"] == "observe"
    assert "control" not in specs["old"].to_dict()


def test_control_is_validated() -> None:
    with pytest.raises(DeviceConfigError) as exc:
        parse_devices({
            "bad": {**BATTERY, "control": "maybe"},
            "meter": {"category": "meter", "type": "tasmota", "control": "observe"},
        })
    text = "\n".join(exc.value.errors)
    assert "'control:' must be one of observe, active" in text
    assert "'control:' is only for devices Energy Assistant commands" in text


def test_control_round_trips_through_the_form() -> None:
    form = entry_to_form("bat", {**BATTERY, "control": "observe"})
    assert form["control"] == "observe"
    assert form_to_entry(form)["control"] == "observe"
    assert "control" not in form_to_entry(entry_to_form("bat", BATTERY))


# ── control loop: nothing is sent to an observed device ──────────────────────


class _Refuses:
    """A device that fails the test on any command."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id

    async def send_command(self, command: Any) -> None:
        pytest.fail(f"command sent to observed device {self.device_id}: {command}")


async def test_observed_devices_get_no_command_but_report_what_would_be_sent() -> None:
    sent: list[Any] = []

    async def record(command: Any) -> None:
        sent.append(command)

    devices = {"obs": _Refuses("obs"), "act": SimpleNamespace(send_command=record)}
    loop = ControlLoop(ledger=BatteryCostLedger(), contributors=[  # type: ignore[list-item]
        SimpleNamespace(device_id="obs"), SimpleNamespace(device_id="act"),
    ])
    loop._active_plan = SimpleNamespace()  # type: ignore[assignment]
    loop._compute_setpoints = lambda live: [  # type: ignore[method-assign]
        ("obs", -800.0, "discharge", None), ("act", 500.0, "charge", None),
    ]
    loop.behaviours = lambda rows, live: {"obs": None, "act": None}  # type: ignore[method-assign]
    live = SimpleNamespace(device_states={}, dt_hours=0.0, current_price_eur_per_kwh=0.3)

    await loop.tick(live, SimpleNamespace(get=devices.get), observed={"obs"})  # type: ignore[arg-type]

    assert [c.value for c in sent] == [500.0]
    assert loop.last_setpoints == {"obs": -800.0, "act": 500.0}


def test_execution_log_marks_would_send_slots() -> None:
    log = ExecutionLog(step_minutes=15)
    for i in range(31):
        finished = log.record(
            NOW + timedelta(seconds=30 * i),
            {
                "obs": DeviceTick("discharge", -800.0, -800.0, 0.0, observed=True),
                "act": DeviceTick("charge", 500.0, 500.0, 480.0),
            },
            NOW,
        )
    by_id = {s.device_id: s for s in finished}
    assert by_id["obs"].observed and by_id["obs"].setpoint_w == pytest.approx(-800.0)
    assert not by_id["act"].observed


async def test_execution_rows_keep_the_observed_flag(tmp_path: Path) -> None:
    from energy_assistant.core.execution_log import ExecutionSlot
    from energy_assistant.storage.sqlite import SqliteStorageBackend

    storage = SqliteStorageBackend(tmp_path / "db.sqlite")
    await storage.start()
    try:
        slot = ExecutionSlot(start=NOW, device_id="obs", seconds=900.0, setpoint_ws=-800.0 * 900,
                             setpoint_s=900.0, observed_s=900.0)
        await storage.save_execution_slots([slot])
        rows = await storage.query_execution(NOW, NOW + timedelta(hours=1))
        assert rows[0]["observed"] is True and rows[0]["setpoint_w"] == pytest.approx(-800.0)
    finally:
        await storage.stop()


# ── projection: the observed devices follow the control loop ─────────────────


def _battery() -> StorageConstraints:
    return StorageConstraints(
        device_id="bat", capacity_kwh=10.0, max_charge_kw=2.0, max_discharge_kw=2.0,
        charge_efficiency=1.0, discharge_efficiency=1.0, min_soc_pct=10.0, max_soc_pct=100.0,
    )


def _sample(ts: datetime, *, site_kw: float = 1.0, bat_kw: float = 0.0, stored: float = 5.0,
            ev_kw: float = 0.0, dev_kw: float | None = 0.0) -> LiveSample:
    return LiveSample(
        timestamp=ts, site_net_kw=site_kw, storage_kw={"bat": bat_kw}, stored_kwh={"bat": stored},
        ev={"car": EvLive(plugged=True, power_kw=ev_kw, max_kw=11.0)}, secondary=[],
        import_price=0.30, export_price=0.08,
        devices={"cooler": DeviceLive(power_kw=dev_kw, value=24.0)},
    )


def _cooler() -> ThresholdConstraints:
    return ThresholdConstraints(
        device_id="cooler", bottom_threshold=23.0, top_threshold=25.0, unit="°C",
        direction="reduces", rated_power_kw=0.2, active_rate_per_h=1.0, drift_rate_per_h=0.5,
    )


def test_projection_moves_observed_devices_and_the_grid() -> None:
    proj = ObservedProjection([_battery()], [_cooler()])
    proj.project(_sample(NOW), {"bat", "car", "cooler"}, {})

    out = proj.project(
        _sample(NOW + timedelta(hours=1), site_kw=1.0, bat_kw=0.0, stored=5.0),
        {"bat", "car", "cooler"},
        {"bat": -1000.0, "car": 3000.0, "cooler": 200.0},
    )

    assert out.storage_kw["bat"] == pytest.approx(-1.0)
    assert out.stored_kwh["bat"] == pytest.approx(4.0)
    assert out.ev["car"].power_kw == pytest.approx(3.0)
    assert out.devices["cooler"].power_kw == pytest.approx(0.2)
    # -1 kW battery, +3 kW car, +0.2 kW cooler on top of the measured 1 kW import
    assert out.site_net_kw == pytest.approx(1.0 - 1.0 + 3.0 + 0.2)


def test_projection_stops_a_battery_at_its_soc_limit_and_leaves_active_devices() -> None:
    proj = ObservedProjection([_battery()])
    proj.project(_sample(NOW, stored=1.5), {"bat"}, {})
    out = proj.project(_sample(NOW + timedelta(hours=1), stored=1.5), {"bat"}, {"bat": -2000.0})
    assert out.storage_kw["bat"] == pytest.approx(-0.5)   # only down to the 10 % minimum
    assert out.stored_kwh["bat"] == pytest.approx(1.0)

    out = proj.project(_sample(NOW + timedelta(hours=2), ev_kw=7.0), set(), {"car": 0.0})
    assert out.ev["car"].power_kw == 7.0 and out.site_net_kw == 1.0


def test_projected_house_saves_what_the_observed_battery_would_have() -> None:
    """Measured, the observed battery sits empty; projected, it charges at a
    cheap price and covers the load at an expensive one — so the projected
    house shows a saving the real accounting does not."""
    proj = ObservedProjection([_battery()])
    tracker = SavingsTracker([_battery()], step_minutes=15)
    t = NOW
    plan = {"bat": 2000.0}
    slots = []
    for i in range(121):
        t = NOW + timedelta(seconds=60 * i)
        expensive = i > 60
        if expensive:
            plan = {"bat": -1000.0}
        sample = LiveSample(
            timestamp=t, site_net_kw=1.0, storage_kw={"bat": 0.0}, stored_kwh={"bat": 1.0},
            ev={}, secondary=[], import_price=0.40 if expensive else 0.10, export_price=0.05,
            pv_kw=0.0,
        )
        slots += tracker.tick(proj.project(sample, {"bat"}, plan))
    # 2 kWh charged at 0.10 € on top of 1 kWh at no cost (the minimum SoC):
    # 1 kWh of it at 0.20 € / 3 kWh replaces grid energy at 0.40 €.
    assert sum(s.saving for s in slots) == pytest.approx(0.40 - 0.20 / 3, abs=0.01)


# ── API: new devices observe, the switch applies without a reload ───────────

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
    type: modbus
    profile: sma_sunny_boy_storage
    host: 127.0.0.1:9
    metered_by: grid_meter
    params:
      capacity_kwh: 7.4
      max_charge_kw: 3.0
      max_discharge_kw: 3.0
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def running(tmp_path: Path, monkeypatch) -> AsyncIterator[tuple[Any, httpx.AsyncClient, Path]]:
    from energy_assistant.server import Application

    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
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


async def test_existing_devices_stay_active(running) -> None:
    _app, client, _config = running
    status = (await client.get("/api/status")).json()
    assert status["control"] == {"home_battery": "active"}


async def test_a_new_battery_starts_in_observe_mode(running) -> None:
    app, client, config = running
    new = {
        "id": "garage_battery", "category": "battery", "type": "modbus",
        "settings": {"profile": "sma_sunny_boy_storage", "host": "127.0.0.1:9"},
        "params": {"capacity_kwh": 5, "max_charge_kw": 2, "max_discharge_kw": 2},
        "metered_by": "grid_meter",
    }
    resp = await client.put("/api/config/devices/garage_battery", json={"value": new, "create": True})
    assert resp.status_code == 200, resp.text
    assert "    control: observe\n" in config.read_text()
    assert app.control_modes()["garage_battery"] == "observe"
    assert "garage_battery" in app._observed


async def test_switching_control_writes_the_file_and_applies_without_reload(
    running, monkeypatch
) -> None:
    app, client, config = running
    reloads: list[int] = []
    released: list[str] = []

    async def no_reload() -> None:
        reloads.append(1)

    async def release(device: Any) -> None:
        released.append(device.device_id)

    monkeypatch.setattr(app, "reload_config", no_reload)
    monkeypatch.setattr(app, "_release_device", release)
    app._dry_run = False  # the release is skipped in a dry run

    resp = await client.put("/api/config/devices/home_battery/control", json={"control": "observe"})
    assert resp.status_code == 200, resp.text
    assert "    control: observe\n" in config.read_text()
    assert app._observed == {"home_battery"}
    assert released == ["home_battery"]
    state = (await client.get("/api/config/state")).json()
    assert state["pending_reload"] is False

    resp = await client.put("/api/config/devices/home_battery/control", json={"control": "active"})
    assert resp.status_code == 200, resp.text
    assert "    control: active\n" in config.read_text()
    assert app._observed == frozenset()
    assert released == ["home_battery"]   # switching on releases nothing
    assert reloads == []
    app._dry_run = True

    bad = await client.put("/api/config/devices/grid_meter/control", json={"control": "observe"})
    assert bad.status_code == 422


async def test_observed_devices_are_not_released_at_stop() -> None:
    from energy_assistant.server import Application

    released: list[str] = []

    class _Dev:
        def __init__(self, device_id: str) -> None:
            self.device_id = device_id

        async def release(self) -> None:
            released.append(self.device_id)

    specs = parse_devices({
        "obs": {**BATTERY, "control": "observe"},
        "act": BATTERY,
    })
    app = Application.__new__(Application)
    app._dry_run = False
    app._cfg = SimpleNamespace(devices=specs)
    app._observed = app._observed_devices()
    app._registry = SimpleNamespace(all=lambda: [_Dev("obs"), _Dev("act")])
    await app._release_devices()
    await asyncio.sleep(0)
    assert released == ["act"]


def test_the_switch_of_a_device_shows_where_it_is_controlled_through() -> None:
    """The aquarium plug and a car have no control: of their own; their
    pages show the switch of the cooler and of the charger."""
    from energy_assistant.server import Application

    specs = parse_devices({
        "aquarium_plug": {"category": "switch", "type": "homeassistant_switch",
                          "params": {"rated_power_w": 200}},
        "aquarium_cooler": {
            "category": "thermal_load", "type": "homeassistant_thermal",
            "controlled_by": "aquarium_plug", "control": "observe",
            "params": {"bottom_threshold": 24, "top_threshold": 25,
                       "active_rate_per_h": 1.0, "drift_rate_per_h": 0.3},
        },
        "car": {"category": "vehicle", "type": "homeassistant_vehicle",
                "params": {"capacity_kwh": 60}},
        "wallbox": {"category": "charger", "type": "openwb_mqtt",
                    "params": {"default_vehicle": "car"}},
        "garage": {"category": "charger", "type": "openwb_mqtt"},
    })
    app = Application.__new__(Application)
    app._cfg = SimpleNamespace(devices=specs)
    at: dict[str, str | None] = {"car": None}
    app._fleet = SimpleNamespace(
        chargers={d: s for d, s in specs.items() if s.category.value == "charger"},
        sessions=SimpleNamespace(charger_of=lambda vid: at.get(vid)),
    )

    assert app.control_targets("aquarium_cooler") == ["aquarium_cooler"]
    assert app.control_targets("aquarium_plug") == ["aquarium_cooler"]
    assert app.effective_control("aquarium_plug") == "observe"
    assert app.control_targets("car") == ["wallbox"]      # its default charger
    at["car"] = "garage"
    assert app.control_targets("car") == ["garage"]       # where it is plugged in
    assert app.effective_control("car") == "active"

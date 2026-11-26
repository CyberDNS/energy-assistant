"""Fail-safe release (#135): every controlled device goes back to its own
operation at stop, reload, a stale plan and a stuck control loop."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import pkgutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import nornwatt.plugins as plugins_pkg
from nornwatt.core import failsafe as fs
from nornwatt.core.failsafe import FailSafe
from nornwatt.core.models import DeviceCommand
from nornwatt.plugins import registry as plugin_registry

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _plan(created_at: datetime = NOW, *, slots: int = 4, start: datetime = NOW) -> Any:
    intents = [SimpleNamespace(timestep=start + timedelta(minutes=15 * i)) for i in range(slots)]
    return SimpleNamespace(created_at=created_at, step_minutes=15, intents=intents)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _failsafe(clock: _Clock | None = None) -> tuple[FailSafe, list[int]]:
    calls: list[int] = []

    async def release_all() -> None:
        calls.append(1)

    return FailSafe(release_all, plan_stale_after_s=45 * 60, control_stuck_after_s=600,
                    clock=clock or _Clock(), now=lambda: NOW), calls


# ── FailSafe ──────────────────────────────────────────────────────────────────


def test_defaults_tolerate_one_failed_plan_and_scale_with_the_intervals() -> None:
    assert fs.default_plan_stale_after_s(900) == 45 * 60
    assert fs.default_plan_stale_after_s(3600) == 3 * 3600
    assert fs.default_control_stuck_after_s(30) == 600
    assert fs.default_control_stuck_after_s(120) == 1200


async def test_released_at_start_until_the_first_plan() -> None:
    safe, calls = _failsafe()
    assert safe.released and safe.reason == fs.START
    assert await safe.check_plan(None, NOW) is False
    assert calls == []  # the application released at start already
    assert await safe.check_plan(_plan(), NOW) is True
    assert not safe.released and not safe.events  # the start is not an event


async def test_stale_plan_releases_once_and_a_fresh_plan_takes_over() -> None:
    safe, calls = _failsafe()
    await safe.check_plan(_plan(), NOW)

    old = _plan(NOW - timedelta(minutes=46))
    assert await safe.check_plan(old, NOW) is False
    assert await safe.check_plan(old, NOW + timedelta(seconds=30)) is False
    assert calls == [1]
    assert safe.released and safe.reason == fs.PLAN_STALE
    assert "46 min old" in safe.detail

    assert await safe.check_plan(_plan(NOW), NOW + timedelta(minutes=1)) is True
    assert not safe.released
    assert [e.kind for e in safe.events] == ["released", "taken_over"]
    status = safe.to_dict()
    assert status["released"] is False
    assert [e["kind"] for e in status["events"]] == ["taken_over", "released"]  # newest first


async def test_a_plan_that_ended_is_stale() -> None:
    safe, calls = _failsafe()
    plan = _plan(NOW, slots=2)  # covers 12:00–12:30
    assert await safe.check_plan(plan, NOW + timedelta(minutes=29)) is True
    assert await safe.check_plan(plan, NOW + timedelta(minutes=30)) is False
    assert calls == [1] and "ended" in safe.detail


async def test_plan_lost_after_takeover_releases() -> None:
    safe, calls = _failsafe()
    await safe.check_plan(_plan(), NOW)
    assert await safe.check_plan(None, NOW) is False
    assert calls == [1] and safe.reason == fs.PLAN_STALE


async def test_stuck_control_loop_releases_and_the_next_tick_takes_over() -> None:
    clock = _Clock()
    safe, calls = _failsafe(clock)
    await safe.check_plan(_plan(), NOW)
    safe.tick_done()

    clock.t = 599.0
    await safe.check_stuck()
    assert calls == []
    clock.t = 601.0
    await safe.check_stuck()
    await safe.check_stuck()
    assert calls == [1]
    assert safe.released and safe.reason == fs.CONTROL_STUCK

    safe.tick_done()
    assert not safe.released
    assert [(e.kind, e.reason) for e in safe.events] == [
        ("released", fs.CONTROL_STUCK), ("taken_over", fs.CONTROL_STUCK)]


async def test_watchdog_runs_independently_of_the_control_loop() -> None:
    clock = _Clock()
    calls: list[int] = []

    async def release_all() -> None:
        calls.append(1)

    safe = FailSafe(release_all, plan_stale_after_s=2700, control_stuck_after_s=600,
                    check_interval_s=0.01, clock=clock)
    task = asyncio.create_task(safe.watch())
    await asyncio.sleep(0.03)
    assert calls == []
    clock.t = 700.0  # no tick_done() — the control loop hangs
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == [1]


async def test_watchdog_survives_a_failing_release(caplog) -> None:
    clock = _Clock()

    async def release_all() -> None:
        raise RuntimeError("boom")

    safe = FailSafe(release_all, plan_stale_after_s=2700, control_stuck_after_s=600,
                    check_interval_s=0.01, clock=clock)
    task = asyncio.create_task(safe.watch())
    await asyncio.sleep(0.02)
    clock.t = 700.0
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert "watchdog check failed" in caplog.text


# ── ControlLoop: no commands while released ───────────────────────────────────


async def test_control_loop_sends_nothing_while_released() -> None:
    from nornwatt.core.control import ControlLoop
    from nornwatt.core.ledger import BatteryCostLedger

    sent: list[DeviceCommand] = []
    device = SimpleNamespace(send_command=lambda c: sent.append(c) or asyncio.sleep(0))
    contributor = SimpleNamespace(device_id="bat")
    loop = ControlLoop(ledger=BatteryCostLedger(), contributors=[contributor])  # type: ignore[list-item]
    loop._active_plan = SimpleNamespace()  # type: ignore[assignment]
    loop._compute_setpoints = lambda live: [("bat", 500.0, "charge", None)]  # type: ignore[method-assign]
    loop.behaviours = lambda rows, live: {"bat": None}  # type: ignore[method-assign]
    registry = SimpleNamespace(get=lambda did: device)
    live = SimpleNamespace(device_states={}, dt_hours=0.0, current_price_eur_per_kwh=0.3)

    await loop.tick(live, registry, send_commands=False)  # type: ignore[arg-type]
    assert sent == [] and loop.last_setpoints == {}
    await loop.tick(live, registry)  # type: ignore[arg-type]
    assert [c.value for c in sent] == [500.0]


# ── Application: stop, reload, stale plan, stuck loop → release ──────────────


class _Device:
    def __init__(self, device_id: str, *, fail: bool = False, hang: bool = False) -> None:
        self.device_id = device_id
        self.released = 0
        self.fail, self.hang = fail, hang

    async def release(self) -> None:
        self.released += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise ConnectionError("offline")


def _app(devices: list[Any]) -> Any:
    from nornwatt.server import Application

    app = Application.__new__(Application)
    app._dry_run = False
    app._registry = SimpleNamespace(all=lambda: devices)
    app.tasks = []
    app._runtime_tasks = []
    app._plan_active_count = 0
    app._mqtt_publisher = None
    app._config_problems = []
    return app


def _devices() -> list[Any]:
    return [_Device("sma"), _Device("zendure", fail=True), _Device("wallbox"),
            _Device("plug"), SimpleNamespace(device_id="meter")]


async def test_stop_releases_every_device() -> None:
    devices = _devices()
    await _app(devices).stop()
    assert [d.released for d in devices if isinstance(d, _Device)] == [1, 1, 1, 1]


async def test_reload_releases_every_device() -> None:
    devices = _devices()
    app = _app(devices)
    saved: list[Any] = []

    async def save(state: Any) -> None:
        saved.append(state)

    app._storage = SimpleNamespace(save_savings_state=save)
    app._savings = SimpleNamespace(state=SimpleNamespace(to_dict=dict))
    await app._stop_runtime()
    assert [d.released for d in devices if isinstance(d, _Device)] == [1, 1, 1, 1]


async def test_a_hanging_device_does_not_block_the_others(monkeypatch, caplog) -> None:
    from nornwatt import server

    monkeypatch.setattr(server, "_RELEASE_TIMEOUT_S", 0.05)
    hanging, ok = _Device("wallbox", hang=True), _Device("sma")
    await _app([hanging, ok])._release_devices()
    assert ok.released == 1
    assert "no answer within" in caplog.text


async def test_stale_plan_in_the_control_tick_releases_every_device() -> None:
    devices = _devices()
    app = _app(devices)
    app._failsafe = FailSafe(app._release_devices, plan_stale_after_s=2700,
                             control_stuck_after_s=600)
    await app._failsafe.check_plan(_plan(datetime.now(UTC)), datetime.now(UTC))
    stale = _plan(datetime.now(UTC) - timedelta(hours=1))
    assert await app._failsafe.check_plan(stale, datetime.now(UTC)) is False
    assert [d.released for d in devices if isinstance(d, _Device)] == [1, 1, 1, 1]


async def test_stuck_control_loop_releases_every_device() -> None:
    devices = _devices()
    app = _app(devices)
    clock = _Clock()
    app._failsafe = FailSafe(app._release_devices, plan_stale_after_s=2700,
                             control_stuck_after_s=600, clock=clock)
    clock.t = 601.0
    await app._failsafe.check_stuck()
    assert [d.released for d in devices if isinstance(d, _Device)] == [1, 1, 1, 1]


# ── Every plugin that accepts commands can be released ───────────────────────

#: Device classes that take no commands (``send_command`` ignores them), or
#: send them through another device that is released itself.
_NO_RELEASE = {
    "DifferentialDevice": "derived, read only",
    "GenericConsumerDevice": "virtual, read only",
    "HASensorDevice": "read only",
    "HAVehicleDevice": "charged through its charger",
    "TasmotaDevice": "read only",
    "TibberDevice": "read only",
    "TibberLocalDevice": "read only",
    "ThermalLoadDevice": "switched through its homeassistant_switch, which is released",
}
#: Types whose factory builds another plugin's device.
_DELEGATES = {"openwb_homeassistant": "homeassistant_charger"}


def _device_classes(package: str) -> list[type]:
    pkg = importlib.import_module(package)
    modules = [pkg] + [
        importlib.import_module(m.name)
        for m in pkgutil.walk_packages(pkg.__path__, package + ".")
    ]
    return [
        cls for m in modules for _n, cls in inspect.getmembers(m, inspect.isclass)
        if cls.__module__ == m.__name__
        and callable(getattr(cls, "send_command", None))
        and callable(getattr(cls, "get_state", None))
    ]


def test_every_plugin_that_accepts_commands_implements_release() -> None:
    seen: set[str] = set()
    for info in plugin_registry.device_infos():
        package = info.factory.__module__
        assert package.startswith(plugins_pkg.__name__ + ".")
        classes = _device_classes(package)
        if info.type_name in _DELEGATES:
            assert not classes
            target = plugin_registry.device_info(_DELEGATES[info.type_name])
            assert target is not None
            classes = _device_classes(target.factory.__module__)
        assert classes, f"{info.type_name}: no device class found"
        for cls in classes:
            seen.add(cls.__name__)
            if cls.__name__ in _NO_RELEASE:
                assert not callable(getattr(cls, "release", None)), cls
            else:
                assert inspect.iscoroutinefunction(getattr(cls, "release", None)), (
                    f"{info.type_name}: {cls.__name__} accepts commands but has no "
                    "async release() (or add it to _NO_RELEASE with the reason)")
    assert set(_NO_RELEASE) <= seen, set(_NO_RELEASE) - seen


# ── homeassistant_switch ─────────────────────────────────────────────────────


def _switch(state: str, release_state: str = "on") -> tuple[Any, Any]:
    from helpers.fake_ha_client import FakeHAClient

    from nornwatt.plugins._homeassistant.actuators import SwitchActuator
    from nornwatt.plugins.homeassistant_switch.device import HASwitchDevice

    client = FakeHAClient({"switch.plug": state})
    actuator = SwitchActuator("plug", client, "switch.plug")
    return HASwitchDevice("plug", client, actuator, release_state=release_state), client


@pytest.mark.parametrize(("state", "release_state", "calls"), [
    ("off", "on", [("homeassistant", "turn_on", {"entity_id": "switch.plug"})]),
    ("on", "on", []),
    ("on", "off", [("homeassistant", "turn_off", {"entity_id": "switch.plug"})]),
    ("off", "leave", []),
])
async def test_switch_release_state(state: str, release_state: str, calls: list) -> None:
    device, client = _switch(state, release_state)
    await device.release()
    assert client.calls == calls


def test_switch_release_state_is_a_setting() -> None:
    from helpers.fake_ha_client import FakeHAClient

    from nornwatt.plugins.homeassistant_switch import _build

    ctx = SimpleNamespace(ha_client=FakeHAClient())
    cfg = {"capabilities": {"switch": {"entity": "switch.plug"}}}
    assert _build("plug", cfg, ctx)._release_state == "on"  # type: ignore[arg-type,union-attr]
    cfg["release_state"] = "off"
    assert _build("plug", cfg, ctx)._release_state == "off"  # type: ignore[arg-type,union-attr]
    spec = plugin_registry.device_info("homeassistant_switch").settings["release_state"]  # type: ignore[union-attr,index]
    assert spec.choices == ("on", "off", "leave") and spec.default == "on"


# ── zendure ──────────────────────────────────────────────────────────────────


def _zendure(tmp_path, **kw) -> tuple[Any, Any]:
    from test_zendure_plugin import FakeBridge, _device

    bridge = FakeBridge()
    return _device(bridge, state_path=tmp_path / "zendure" / "zendure.json", **kw), bridge


def _zreport(bridge: Any, **props: Any) -> None:
    from test_zendure_plugin import _report

    _report(bridge, **props)


@pytest.fixture
def _zclock(monkeypatch) -> None:
    monkeypatch.setattr("nornwatt.plugins.zendure.device.time.monotonic", lambda: 100.0)


async def test_zendure_release_returns_to_the_mode_before_the_takeover(tmp_path, _zclock) -> None:
    device, bridge = _zendure(tmp_path)
    _zreport(bridge, autoModel=8, acMode=2, inputLimit=0, outputLimit=300)
    await device.send_command(DeviceCommand(device_id="zendure", command="set_power_w",
                                            value=1000))
    assert {"autoModel": 0} in bridge.writes
    _zreport(bridge, autoModel=0, acMode=1, inputLimit=1000, outputLimit=0)
    bridge.writes.clear()

    await device.release()
    assert bridge.writes == [{"inputLimit": 0}, {"autoModel": 8}]
    saved = json.loads((tmp_path / "zendure" / "zendure.json").read_text())
    assert saved == {"own_auto_model": 8}

    # After a crash and restart: the device is still manual, the mode is known.
    restarted, bridge2 = _zendure(tmp_path)
    _zreport(bridge2, autoModel=0, acMode=1, inputLimit=1000, outputLimit=0)
    await restarted.release()
    assert bridge2.writes == [{"inputLimit": 0}, {"autoModel": 8}]


async def test_zendure_configured_mode_wins(tmp_path, _zclock) -> None:
    device, bridge = _zendure(tmp_path, release_auto_model=9)
    _zreport(bridge, autoModel=8)
    await device.send_command(DeviceCommand(device_id="zendure", command="set_power_w", value=0))
    bridge.writes.clear()
    await device.release()
    assert bridge.writes[-1] == {"autoModel": 9}


async def test_zendure_unknown_mode_releases_to_idle(tmp_path, _zclock, caplog) -> None:
    device, bridge = _zendure(tmp_path)
    _zreport(bridge, autoModel=0, acMode=2, inputLimit=0, outputLimit=500)
    await device.release()
    assert bridge.writes == [{"outputLimit": 0}]
    assert "release_auto_model" in caplog.text


async def test_zendure_release_needs_the_connection(tmp_path, _zclock) -> None:
    device, bridge = _zendure(tmp_path)
    bridge.is_connected = False
    with pytest.raises(ConnectionError):
        await device.release()


# ── openwb_mqtt ──────────────────────────────────────────────────────────────


@pytest.fixture
def _no_openwb_delay(monkeypatch) -> None:
    from nornwatt.plugins.openwb_mqtt import device as device_module

    monkeypatch.setattr(device_module, "_INTER_PUBLISH_DELAY_S", 0.0)


async def test_openwb_release_puts_back_instant_values_then_pv(_no_openwb_delay) -> None:
    from test_openwb_mqtt import (
        FakeBridge,
        _cmd,
        _device,
        _instant_section,
        _template,
    )

    from nornwatt.core.models import Behaviour

    bridge = FakeBridge({("5", "charge_template"): _template(current=10, soc=90),
                         ("5", "chargemode"): "stop", ("5", "plug_state"): "True"})
    device = _device(bridge)
    device.update_target_soc(80)
    await device.send_command(_cmd(4140, Behaviour.CHARGE))
    written = bridge.published[0][2]
    assert _instant_section(written)["current"] == 6
    # openWB now reports what was written.
    bridge.values_map[("5", "charge_template")] = written
    bridge.values_map[("5", "chargemode")] = "instant_charging"
    bridge.published.clear()

    await device.release()
    assert [k for _cp, k, _p in bridge.published] == ["template", "chargemode"]
    restored = _instant_section(bridge.published[0][2])
    assert restored["current"] == 10 and restored["limit"]["soc"] == 90
    assert bridge.published[1][2] == "pv"

    # Nothing left to put back: a second release only checks the mode.
    bridge.values_map[("5", "chargemode")] = "pv_charging"
    bridge.published.clear()
    await device.release()
    assert bridge.published == []


async def test_openwb_release_mode_leave_and_unplug(_no_openwb_delay) -> None:
    from test_openwb_mqtt import FakeBridge, _cmd, _device, _template

    from nornwatt.core.models import Behaviour

    bridge = FakeBridge({("5", "charge_template"): _template(current=10),
                         ("5", "chargemode"): "stop", ("5", "plug_state"): "True"})
    device = _device(bridge, release_mode="leave")
    await device.send_command(_cmd(7000, Behaviour.CHARGE))
    # The car leaves: openWB resets the template itself.
    bridge.values_map[("5", "plug_state")] = "False"
    await device.get_state()
    bridge.published.clear()
    await device.release()
    assert bridge.published == []

    with pytest.raises(ValueError, match="release_mode"):
        _device(bridge, release_mode="eco")


async def test_openwb_homeassistant_releases_to_pv_charging() -> None:
    from nornwatt.plugins.openwb_homeassistant import translate

    cfg = translate({"entity_mode": "select.cp_mode"})
    assert "release" not in cfg["capabilities"]["power_setpoint"]
    from helpers.fake_ha_client import FakeHAClient

    from nornwatt.core.models import Behaviour
    from nornwatt.plugins.homeassistant_charger.control import parse_control

    control = parse_control("cp", FakeHAClient(), cfg["capabilities"]["power_setpoint"])
    assert control._release is Behaviour.ABSORB_SURPLUS


# ── SIGTERM: devices are released while their connections still run ─────────


async def test_sigterm_releases_before_the_connections_close(monkeypatch) -> None:
    import os
    import signal

    from nornwatt import __main__ as main_module

    connection = asyncio.create_task(asyncio.Event().wait(), name="mqtt-bridge")
    seen: dict[str, bool] = {}

    class FakeApp:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def run_forever(self) -> None:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            finally:
                # Application.stop() releases the devices here (and awaits).
                await asyncio.sleep(0.01)
                seen["connection_alive"] = not connection.done()

    monkeypatch.setattr(main_module, "Application", FakeApp)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(asyncio.create_task(main_module._run(None, None)), 5)  # type: ignore[arg-type]
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        connection.cancel()
    assert seen == {"connection_alive": True}

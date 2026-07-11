"""Tests for the openwb_mqtt plugin — direct simpleAPI control."""

from __future__ import annotations

import json

import pytest

from energy_assistant.core.config import MqttConfig
from energy_assistant.core.models import DeviceCommand, DeviceRole
from energy_assistant.plugins.openwb_mqtt import device as device_module
from energy_assistant.plugins.openwb_mqtt.device import (
    OpenWBMqttBridge,
    OpenWBMqttDevice,
)


@pytest.fixture(autouse=True)
def _no_publish_delay(monkeypatch) -> None:
    """Skip the anti-race pacing between publishes in unit tests."""
    monkeypatch.setattr(device_module, "_INTER_PUBLISH_DELAY_S", 0.0)


class FakeBridge:
    """In-memory stand-in for OpenWBMqttBridge."""

    def __init__(self, values: dict[tuple[str, str], str] | None = None) -> None:
        self.values_map = dict(values or {})
        self.published: list[tuple[str, str, str]] = []  # (cp_id, key, payload)
        self.connected = True

    def ensure_started(self) -> None:
        pass

    def value(self, cp_id: str, key: str, max_age_s: float | None = None) -> str | None:
        return self.values_map.get((cp_id, key))

    async def publish_set(self, cp_id: str, key: str, payload: str) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        self.published.append((cp_id, key, payload))

    async def publish_charge_template(self, cp_id: str, template_json: str) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        self.published.append((cp_id, "template", template_json))


def _template(current: int = 10, selected: str = "soc", soc: int = 90) -> str:
    """A minimal charge template as retained by openWB."""
    return json.dumps({
        "id": 1,
        "name": "Test",
        "chargemode": {
            "selected": "stop",
            "instant_charging": {
                "current": current,
                "limit": {"selected": selected, "amount": 1000, "soc": soc},
                "phases_to_use": 3,
            },
        },
    })


def _instant_section(payload: str) -> dict:
    return json.loads(payload)["chargemode"]["instant_charging"]


def _device(bridge: FakeBridge, **kwargs) -> OpenWBMqttDevice:
    return OpenWBMqttDevice("wallbox", bridge, chargepoint_id=5, **kwargs)


def _cmd(value: float) -> DeviceCommand:
    return DeviceCommand(device_id="wallbox", command="set_power_w", value=value)


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_maps_simpleapi_values() -> None:
    # Payloads as observed on a live openWB 2.x: SoC nested under soc/soc,
    # Python-style booleans, long chargemode names.
    bridge = FakeBridge({
        ("5", "soc/soc"): "58",
        ("5", "power"): "10850",
        ("5", "plug_state"): "True",
        ("5", "chargemode"): "pv_charging",
    })
    state = await _device(bridge).get_state()

    assert state.soc_pct == 58.0
    assert state.power_w == 10850.0
    assert state.available is True
    assert state.extra == {"plugged": True, "chargemode": "pv"}


@pytest.mark.asyncio
async def test_get_state_falls_back_to_pro_soc() -> None:
    bridge = FakeBridge({
        ("5", "soc/soc"): "null",  # vehicle SoC unavailable (JSON null payload)
        ("5", "pro_soc"): "63",
        ("5", "plug_state"): "True",
    })
    state = await _device(bridge).get_state()
    assert state.soc_pct == 63.0
    assert state.available is True


@pytest.mark.asyncio
async def test_get_state_unavailable_when_unplugged_or_no_soc() -> None:
    bridge = FakeBridge({("5", "soc/soc"): "42", ("5", "plug_state"): "False"})
    assert (await _device(bridge).get_state()).available is False

    bridge = FakeBridge({("5", "plug_state"): "True"})  # no SoC
    assert (await _device(bridge).get_state()).available is False


@pytest.mark.asyncio
async def test_get_state_unavailable_when_bridge_disconnected() -> None:
    bridge = FakeBridge({("5", "soc/soc"): "42", ("5", "plug_state"): "True"})
    bridge.connected = False
    assert (await _device(bridge).get_state()).available is False


def test_role_is_ev_charger() -> None:
    assert _device(FakeBridge()).role == DeviceRole.EV_CHARGER


# ---------------------------------------------------------------------------
# send_command — mode encoding + reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_published_on_mode_mismatch() -> None:
    bridge = FakeBridge({("5", "chargemode"): "instant_charging"})
    await _device(bridge).send_command(_cmd(0.0))
    assert bridge.published == [("5", "chargemode", "stop")]


@pytest.mark.asyncio
async def test_mode_write_skipped_when_already_in_desired_mode() -> None:
    # Read topic reports the long name; set payload is the short one —
    # reconciliation must treat "pv_charging" as matching desired "pv".
    bridge = FakeBridge({("5", "chargemode"): "pv_charging"})
    await _device(bridge).send_command(_cmd(1.0))  # PV sentinel
    assert bridge.published == []


@pytest.mark.asyncio
async def test_instant_writes_template_before_mode() -> None:
    """Instant activation: one atomic template write (current + SoC limit),
    then the chargemode switch."""
    bridge = FakeBridge({
        ("5", "chargemode"): "stop",  # stop has no long form
        ("5", "charge_template"): _template(current=10, selected="none", soc=90),
    })
    dev = _device(bridge)
    dev.update_target_soc(80.0)

    await dev.send_command(_cmd(11_000.0))  # 11 kW / (230 V × 3) ≈ 16 A

    assert [(cp, kind) for cp, kind, _ in bridge.published] == [
        ("5", "template"),
        ("5", "chargemode"),
    ]
    instant = _instant_section(bridge.published[0][2])
    assert instant["current"] == 16
    assert instant["limit"]["selected"] == "soc"
    assert instant["limit"]["soc"] == 80
    assert bridge.published[1][2] == "instant"


@pytest.mark.asyncio
async def test_instant_params_not_republished_when_unchanged() -> None:
    """Template already matches the plan → nothing is published."""
    bridge = FakeBridge({
        ("5", "chargemode"): "instant_charging",
        ("5", "charge_template"): _template(current=16, selected="soc", soc=80),
    })
    dev = _device(bridge)
    dev.update_target_soc(80.0)

    await dev.send_command(_cmd(11_000.0))
    assert bridge.published == []


@pytest.mark.asyncio
async def test_soc_limit_republished_after_template_reset() -> None:
    """openWB resets the temporary charge template on vehicle unplug — the
    limit must be re-applied when the retained template no longer matches."""
    bridge = FakeBridge({
        ("5", "chargemode"): "instant_charging",  # already instant
        ("5", "charge_template"): _template(current=16, selected="soc", soc=90),
    })
    dev = _device(bridge)
    dev.update_target_soc(80.0)

    await dev.send_command(_cmd(11_000.0))

    assert [(cp, kind) for cp, kind, _ in bridge.published] == [("5", "template")]
    assert _instant_section(bridge.published[0][2])["limit"]["soc"] == 80


@pytest.mark.asyncio
async def test_instant_without_template_still_switches_mode() -> None:
    """No retained template yet → current/limit cannot be set this tick,
    but the mode switch must not be blocked."""
    bridge = FakeBridge({("5", "chargemode"): "stop"})
    dev = _device(bridge)
    dev.update_target_soc(80.0)

    await dev.send_command(_cmd(11_000.0))
    assert bridge.published == [("5", "chargemode", "instant")]


@pytest.mark.asyncio
async def test_instant_current_clamped_to_configured_range() -> None:
    bridge = FakeBridge({("5", "charge_template"): _template(current=10)})
    dev = _device(bridge, min_current_a=6, max_current_a=32)

    await dev.send_command(_cmd(30_000.0))  # 30 kW → 43 A → clamped to 32
    assert _instant_section(bridge.published[0][2])["current"] == 32

    bridge.published.clear()
    await dev.send_command(_cmd(600.0))  # 600 W → 1 A → clamped to 6
    assert _instant_section(bridge.published[0][2])["current"] == 6


@pytest.mark.asyncio
async def test_publish_failure_does_not_raise() -> None:
    bridge = FakeBridge({("5", "chargemode"): "instant_charging"})
    bridge.connected = False
    await _device(bridge).send_command(_cmd(0.0))  # must swallow, retried next tick


@pytest.mark.asyncio
async def test_non_power_commands_are_ignored() -> None:
    bridge = FakeBridge()
    await _device(bridge).send_command(
        DeviceCommand(device_id="wallbox", command="reboot", value=1)
    )
    assert bridge.published == []


# ---------------------------------------------------------------------------
# Bridge cache parsing
# ---------------------------------------------------------------------------


def test_bridge_store_parses_chargepoint_topics() -> None:
    bridge = OpenWBMqttBridge(MqttConfig(host="localhost"), "openwbmaster")

    bridge._store("openwbmaster/simpleAPI/chargepoint/5/soc/soc", b"42")
    bridge._store("openwbmaster/simpleAPI/chargepoint/5/powers/1", b"3600")
    bridge._store("openwbmaster/simpleAPI/counter/0/power", b"999")  # not a chargepoint
    bridge._store("openwbmaster/chargepoint/5/set/charge_template", b'{"id": 1}')
    bridge._store("other/topic", b"x")

    assert bridge.value("5", "soc/soc") == "42"
    assert bridge.value("5", "powers/1") == "3600"
    assert bridge.value("5", "charge_template") == '{"id": 1}'
    assert bridge.value("0", "power") is None


@pytest.mark.asyncio
async def test_hours_old_retained_values_still_served(monkeypatch) -> None:
    """openWB read topics are retained + published on change only — a parked
    car may not update soc/plug_state for hours.  The device must NOT treat
    cache age as staleness (regression: 5-min max-age made chargepoints show
    disconnected / SoC-less in the UI after idle periods)."""
    bridge = OpenWBMqttBridge(MqttConfig(host="localhost"), "openwbmaster")
    monkeypatch.setattr(bridge, "ensure_started", lambda: None)
    bridge._store("openwbmaster/simpleAPI/chargepoint/5/soc/soc", b"42")
    bridge._store("openwbmaster/simpleAPI/chargepoint/5/plug_state", b"True")
    # Backdate everything far beyond the old 5-minute guard.
    bridge._cache = {k: (v, ts - 7200.0) for k, (v, ts) in bridge._cache.items()}

    state = await OpenWBMqttDevice("wallbox", bridge, chargepoint_id=5).get_state()
    assert state.soc_pct == 42.0
    assert state.extra["plugged"] is True


def test_bridge_value_respects_max_age() -> None:
    bridge = OpenWBMqttBridge(MqttConfig(host="localhost"), "openwbmaster")
    bridge._store("openwbmaster/simpleAPI/chargepoint/5/soc/soc", b"42")

    assert bridge.value("5", "soc/soc", max_age_s=60.0) == "42"
    # Backdate the cached timestamp beyond the age limit.
    payload, ts = bridge._cache[("5", "soc/soc")]
    bridge._cache[("5", "soc/soc")] = (payload, ts - 120.0)
    assert bridge.value("5", "soc/soc", max_age_s=60.0) is None

"""Tests for the native zendure plugin (cloud login, MQTT bridge, device)."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from energy_assistant.core.config import BackendsConfig, MqttConfig
from energy_assistant.core.device_model import DeviceCategory
from energy_assistant.core.models import DeviceCommand
from energy_assistant.core.plugin_registry import BuildContext, SharedPool
from energy_assistant.plugins import zendure as zendure_plugin
from energy_assistant.plugins.zendure import cloud
from energy_assistant.plugins.zendure.bridge import BrokerParams, ZendureBridge
from energy_assistant.plugins.zendure.device import ZendureDevice

PK, DK = "aBc1dE", "AB12cd34"
REPORT = f"/{PK}/{DK}/properties/report"


def _key(api_url: str = "https://app.zendure.tech/eu", app_key: str = "abc123") -> str:
    return base64.b64encode(f"{api_url}.{app_key}".encode()).decode()


# ── cloud ────────────────────────────────────────────────────────────────────


def test_decode_cloud_key() -> None:
    assert cloud.decode_cloud_key(_key()) == ("https://app.zendure.tech/eu", "abc123")


@pytest.mark.parametrize("bad", ["not base64!!", base64.b64encode(b"nodot").decode()])
def test_decode_cloud_key_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        cloud.decode_cloud_key(bad)


def test_sign_headers() -> None:
    h = cloud.sign_headers({"appKey": "abc"}, timestamp=1700000000, nonce="12345")
    k = cloud._SIGN_KEY
    expected = hashlib.sha1(f"{k}appKeyabcnonce12345timestamp1700000000{k}".encode())
    assert h["sign"] == expected.hexdigest().upper()
    assert h["clientid"] == "zenHa"
    assert h["timestamp"] == "1700000000" and h["nonce"] == "12345"


async def test_cloud_login_parses_response() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["sign"] = request.headers["sign"]
        return httpx.Response(200, json={"code": 200, "data": {
            "mqtt": {"url": "mqtt-eu.zen-iot.com:1883", "clientId": "cid",
                     "username": "u", "password": "p"},
            "deviceList": [{"productKey": PK, "deviceKey": DK, "deviceName": "Hyper",
                            "productModel": "Hyper 2000", "snNumber": "EE1", "online": True}],
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        login = await cloud.cloud_login(_key(), http=http)

    assert seen["url"] == "https://app.zendure.tech/eu/api/ha/deviceList"
    assert seen["body"] == {"appKey": "abc123"}
    assert login.mqtt.host == "mqtt-eu.zen-iot.com" and login.mqtt.port == 1883
    assert login.mqtt.client_id == "cid"
    assert login.devices[0].product_key == PK and login.devices[0].device_key == DK


def test_parse_login_failure() -> None:
    with pytest.raises(RuntimeError, match="invalid appKey"):
        cloud.parse_login_response({"code": 400, "msg": "invalid appKey"})


# ── bridge ───────────────────────────────────────────────────────────────────


async def _no_params() -> BrokerParams:  # pragma: no cover - never connects in tests
    raise AssertionError


def _bridge() -> ZendureBridge:
    b = ZendureBridge("test", _no_params)
    b.register(PK, DK)
    return b


def _report(b: ZendureBridge, now: float = 100.0, topic: str = REPORT, **props) -> None:
    b.handle_message(topic, json.dumps({"properties": props}), now=now)


def test_bridge_merges_delta_reports() -> None:
    b = _bridge()
    _report(b, electricLevel=50, acMode=1)
    _report(b, now=101.0, electricLevel=51)
    props = b.cache(PK, DK).properties
    assert props["electricLevel"] == (51, 101.0)
    assert props["acMode"] == (1, 100.0)
    assert b.cache(PK, DK).last_message == 101.0


def test_bridge_ignores_own_write_echo_and_unknown_devices() -> None:
    b = _bridge()
    _report(b, topic=f"iot/{PK}/{DK}/properties/write", inputLimit=999)
    _report(b, topic="/other/dev/properties/report", inputLimit=999)
    assert b.cache(PK, DK).properties == {}
    assert b.cache(PK, DK).last_message is None


def test_bridge_pack_power_mutually_exclusive() -> None:
    b = _bridge()
    _report(b, packInputPower=400)
    _report(b, now=101.0, outputPackPower=600)
    props = b.cache(PK, DK).properties
    assert props["packInputPower"][0] == 0
    assert props["outputPackPower"][0] == 600


def test_bridge_pack_data() -> None:
    b = _bridge()
    b.handle_message(REPORT, json.dumps({"packData": [{"sn": "A", "socLevel": 5}]}), now=1.0)
    b.handle_message(REPORT, json.dumps({"packData": [{"sn": "A", "power": 10}]}), now=2.0)
    assert b.cache(PK, DK).packs["A"] == {"sn": "A", "socLevel": 5, "power": 10}


# ── device ───────────────────────────────────────────────────────────────────


class FakeBridge(ZendureBridge):
    def __init__(self) -> None:
        super().__init__("fake", _no_params)
        self.is_connected = True
        self.writes: list[dict] = []

    @property
    def connected(self) -> bool:  # type: ignore[override]
        return self.is_connected

    def ensure_started(self) -> None:
        pass

    async def write_properties(self, product_key, device_key, properties) -> None:  # type: ignore[override]
        assert (product_key, device_key) == (PK, DK)
        self.writes.append(properties)


@pytest.fixture
def fake(monkeypatch) -> FakeBridge:
    monkeypatch.setattr("energy_assistant.plugins.zendure.device.time.monotonic", lambda: 100.0)
    return FakeBridge()


def _cmd(command: str, value: float) -> DeviceCommand:
    return DeviceCommand(device_id="zendure", command=command, value=value)


def _device(bridge: ZendureBridge, **kw) -> ZendureDevice:
    return ZendureDevice("zendure", bridge, PK, DK, capacity_kwh=7.6,
                         max_charge_kw=1.2, max_discharge_kw=0.8, **kw)


async def test_state_unavailable_before_first_report(fake: FakeBridge) -> None:
    state = await _device(fake).get_state()
    assert not state.available


async def test_state_unavailable_until_soc_reported(fake: FakeBridge) -> None:
    dev = _device(fake)
    _report(fake, acMode=1)
    assert not (await dev.get_state()).available
    _report(fake, electricLevel=5)
    assert (await dev.get_state()).available


async def test_state_from_report(fake: FakeBridge) -> None:
    dev = _device(fake)
    _report(fake, electricLevel=42, packInputPower=500, outputHomePower=480,
            minSoc=50, socSet=1000, acMode=2, autoModel=0)
    state = await dev.get_state()
    assert state.available
    assert state.power_w == -500
    assert state.soc_pct == 42
    assert state.extra["min_soc_pct"] == 5
    assert state.extra["max_soc_pct"] == 100
    assert dev.category is DeviceCategory.BATTERY
    assert dev.storage_constraints.capacity_kwh == 7.6


async def test_state_unavailable_when_disconnected_or_silent(fake: FakeBridge, monkeypatch) -> None:
    dev = _device(fake)
    _report(fake, now=100.0, electricLevel=42)
    fake.is_connected = False
    assert not (await dev.get_state()).available
    fake.is_connected = True
    monkeypatch.setattr("energy_assistant.plugins.zendure.device.time.monotonic", lambda: 1000.0)
    assert not (await dev.get_state()).available


async def test_stale_power_reads_zero(fake: FakeBridge, monkeypatch) -> None:
    dev = _device(fake)
    _report(fake, now=0.0, outputPackPower=800, electricLevel=60)
    _report(fake, now=390.0, electricLevel=60)  # device alive, power not refreshed
    monkeypatch.setattr("energy_assistant.plugins.zendure.device.time.monotonic", lambda: 400.0)
    state = await dev.get_state()
    assert state.available and state.power_w == 0


async def test_charge_sequence(fake: FakeBridge) -> None:
    dev = _device(fake)
    _report(fake, autoModel=8, acMode=2, inputLimit=0, outputLimit=300)
    await dev.send_command(_cmd("set_power_w", 2000))
    assert fake.writes == [{"autoModel": 0}, {"acMode": 1}, {"inputLimit": 1200}, {"outputLimit": 0}]


async def test_discharge_skips_values_already_reported(fake: FakeBridge) -> None:
    dev = _device(fake)
    _report(fake, autoModel=0, acMode=2, inputLimit=0, outputLimit=300)
    await dev.send_command(_cmd("set_power_w", -500))
    assert fake.writes == [{"outputLimit": 500}]


async def test_idle_and_min_input_limit(fake: FakeBridge) -> None:
    dev = _device(fake)
    await dev.send_command(_cmd("set_power_w", 0))
    assert fake.writes == [{"inputLimit": 0}, {"outputLimit": 0}]
    fake.writes.clear()
    await dev.send_command(_cmd("set_power_w", 10))
    assert {"inputLimit": 30} in fake.writes


async def test_smart_mode_sent_once(fake: FakeBridge) -> None:
    dev = _device(fake, smart_mode=True)
    await dev.send_command(_cmd("set_power_w", 0))
    await dev.send_command(_cmd("set_power_w", 0))
    assert fake.writes.count({"smartMode": 1}) == 1
    assert fake.writes[0] == {"smartMode": 1}


async def test_soc_limits_scaled(fake: FakeBridge) -> None:
    dev = _device(fake)
    await dev.send_command(_cmd("set_charge_limit", 90))
    await dev.send_command(_cmd("set_discharge_limit", 10))
    assert fake.writes == [{"socSet": 900}, {"minSoc": 100}]
    with pytest.raises(ValueError):
        await dev.send_command(_cmd("set_discharge_limit", 80))


# ── factory ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_bridges(monkeypatch) -> None:
    async def _stop(bridge) -> None:
        await bridge.stop()

    monkeypatch.setattr(zendure_plugin, "_bridges", SharedPool(stop=_stop))


def test_build_cloud_accepts_hub_id_and_device_serial() -> None:
    ctx = BuildContext(backends=BackendsConfig())
    dev = zendure_plugin._build("zendure", {
        "cloud_key": _key(), "hub_id": PK, "device_serial": DK,
        "capacity_kwh": 7.6, "max_charge_kw": 1.2, "max_discharge_kw": 0.8,
    }, ctx)
    assert isinstance(dev, ZendureDevice)
    # Devices on the same account share one connection.
    dev2 = zendure_plugin._build("z2", {"cloud_key": _key(), "product_key": PK,
                                        "device_key": "other"}, ctx)
    assert dev2 is not None and dev2._bridge is dev._bridge


def test_build_requires_keys() -> None:
    ctx = BuildContext(backends=BackendsConfig())
    assert zendure_plugin._build("z", {"cloud_key": _key()}, ctx) is None
    assert zendure_plugin._build("z", {"product_key": PK, "device_key": DK}, ctx) is None


def test_build_local_mode() -> None:
    ctx = BuildContext(backends=BackendsConfig(mqtt=MqttConfig(host="broker")))
    dev = zendure_plugin._build("z", {"connection": "local", "product_key": PK,
                                      "device_key": DK}, ctx)
    assert isinstance(dev, ZendureDevice)
    no_mqtt = BuildContext(backends=BackendsConfig())
    assert zendure_plugin._build("z", {"connection": "local", "product_key": PK,
                                       "device_key": DK}, no_mqtt) is None

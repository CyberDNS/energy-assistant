"""Tests for the tibber_local plugin (Tibber Pulse Bridge on the LAN).

Fixtures in ``fixtures/tibber_local`` are SML telegrams recorded from a real bridge
(ISKRA meter, firmware 1279-f0b8c22b), with the meter serial replaced and the
frame CRC recomputed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from smllib.crc import x25

from energy_assistant.core.models import DeviceRole, DeviceState
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.plugins import registry
from energy_assistant.plugins.tibber_local import bridge as bridge_mod
from energy_assistant.plugins.tibber_local.bridge import PulseBridgePoller
from energy_assistant.plugins.tibber_local.decode import TelegramError, decode_telegram
from energy_assistant.plugins.tibber_local.device import TibberLocalDevice

DATA = Path(__file__).parent / "fixtures" / "tibber_local"
VALID = (DATA / "sml_iskra_valid.bin").read_bytes()
VALID_2 = (DATA / "sml_iskra_valid_2.bin").read_bytes()

# 16.7.0 entry in the fixture: ... 62 1b (unit W) 52 00 (scaler 0) 53 02 50 (int16 592)
_POWER_592 = bytes.fromhex("0100100700ff0101621b5200530250")


def _with_power(telegram: bytes, power_w: int) -> bytes:
    """Rewrite OBIS 16.7.0 in a fixture and fix the frame CRC."""
    patched = _POWER_592[:-2] + power_w.to_bytes(2, "big", signed=True)
    out = bytearray(telegram.replace(_POWER_592, patched))
    assert bytes(out) != telegram
    crc = x25.get_crc(bytes(out[:-2]))
    out[-2], out[-1] = crc >> 8, crc & 0xFF
    return bytes(out)


def _crc_error(telegram: bytes) -> bytes:
    out = bytearray(telegram)
    out[200] ^= 0xFF
    return bytes(out)


TS = datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# decoding


def test_decode_recorded_sml_telegram() -> None:
    r = decode_telegram(VALID, received_at=TS)
    assert r.timestamp == TS
    assert r.power_w == 592.0
    assert r.import_kwh == pytest.approx(11076.5928)
    assert r.export_kwh == pytest.approx(2766.0098)
    assert r.frequency_hz == pytest.approx(49.9)
    assert r.phases == pytest.approx({
        "l1_power_w": 277.0, "l1_voltage_v": 226.0, "l1_current_a": 1.91,
        "l2_power_w": 110.0, "l2_voltage_v": 226.3, "l2_current_a": 1.14,
        "l3_power_w": 203.0, "l3_voltage_v": 223.7, "l3_current_a": 1.39,
    })


def test_decode_export_is_negative() -> None:
    assert decode_telegram(_with_power(VALID, -1234)).power_w == -1234.0


def test_receive_time_defaults_to_utc_now() -> None:
    r = decode_telegram(VALID)
    assert r.timestamp.tzinfo is timezone.utc


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_crc_error(VALID), id="crc-error"),
        pytest.param(VALID[:300], id="truncated"),
        pytest.param(VALID[100:], id="missing-start"),
        pytest.param(b"", id="empty"),
        pytest.param(b"<html><body>404 Not Found</body></html>", id="html"),
        pytest.param(b"\xff\xfe\x00garbage", id="binary-garbage"),
    ],
)
def test_bad_telegrams_raise_telegram_error(payload: bytes) -> None:
    with pytest.raises(TelegramError):
        decode_telegram(payload)


def test_decode_plaintext_telegram() -> None:
    text = (
        "/EBZ5DD3BZ06ETA_107\r\n\r\n"
        "1-0:0.0.0*255(1EBZ0100000001)\r\n"
        "1-0:1.8.0*255(000123.4567*kWh)\r\n"
        "1-0:2.8.0*255(000010.0000*kWh)\r\n"
        "1-0:16.7.0*255(-000250.50*W)\r\n"
        "1-0:36.7.0*255(000100.00*W)\r\n"
        "1-0:32.7.0*255(230.1*V)\r\n"
        "!\r\n"
    ).encode()
    r = decode_telegram(text)
    assert r.power_w == -250.5
    assert r.import_kwh == pytest.approx(123.4567)
    assert r.export_kwh == pytest.approx(10.0)
    assert r.phases == {"l1_power_w": 100.0, "l1_voltage_v": 230.1}


def test_decode_plaintext_with_separate_import_export_power() -> None:
    r = decode_telegram(b"1-0:1.7.0*255(0.000*W)\r\n1-0:2.7.0*255(800*W)\r\n")
    assert r.power_w == -800.0


def test_telegram_without_power_is_rejected() -> None:
    with pytest.raises(TelegramError, match="no active power"):
        decode_telegram(b"1-0:1.8.0*255(000123.4567*kWh)\r\n")


# ---------------------------------------------------------------------------
# bridge poller


class _Bridge:
    """MockTransport handler emulating the bridge's HTTP endpoints."""

    def __init__(self) -> None:
        self.telegrams: list[bytes | int] = [VALID]
        self.data_path = "/node_data.json"
        self.nodes: list[dict] | int = [{"node_id": 1, "available": True, "last_data_ms": 900, "rssi": -42}]
        self.password = "secret"
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.url.path}?{request.url.query.decode()}")
        if request.headers.get("authorization") != httpx.BasicAuth("admin", self.password)._auth_header:
            return httpx.Response(401, text="No permission -- see authorization schemes")
        path = request.url.path
        if path == self.data_path and request.url.params.get("node_id") == "1":
            item = self.telegrams[0] if len(self.telegrams) == 1 else self.telegrams.pop(0)
            if isinstance(item, int):
                return httpx.Response(item)
            return httpx.Response(200, content=item, headers={"content-type": "text/text"})
        if path == "/nodes.json":
            if isinstance(self.nodes, int):
                return httpx.Response(self.nodes)
            return httpx.Response(200, json=self.nodes)
        if path == "/node_metrics.json":
            return httpx.Response(200, json={"node": {"node_id": 1, "battery_voltage": 2.69}})
        return httpx.Response(404, text="Not found")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(bridge_mod, "_monotonic", c)
    return c


def _poller(bridge: _Bridge, password: str = "secret") -> tuple[PulseBridgePoller, httpx.AsyncClient]:
    poller = PulseBridgePoller("192.0.2.10", password, poll_interval_s=2, stale_after_s=30)
    client = httpx.AsyncClient(auth=httpx.BasicAuth("admin", password), transport=httpx.MockTransport(bridge))
    return poller, client


async def test_poll_reads_telegram_and_node_info(clock: _Clock) -> None:
    bridge = _Bridge()
    poller, client = _poller(bridge)
    assert await poller.poll_once(client) == 2.0
    reading = poller.latest()
    assert reading is not None and reading.power_w == 592.0
    assert poller.node is not None and poller.node.battery_voltage == 2.69
    assert "/node_data.json?node_id=1" in bridge.requests


async def test_falls_back_to_legacy_data_path(clock: _Clock) -> None:
    bridge = _Bridge()
    bridge.data_path = "/data.json"
    poller, client = _poller(bridge)
    await poller.poll_once(client)
    assert poller.latest() is not None
    bridge.requests.clear()
    clock.now += 2
    await poller.poll_once(client)
    # The working path is remembered.
    assert bridge.requests == ["/data.json?node_id=1"]


async def test_missing_endpoint_logs_error_and_retries_slowly(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _Bridge()
    bridge.data_path = "/gone"
    poller, client = _poller(bridge)
    delay = await poller.poll_once(client)
    assert delay == bridge_mod._NOT_FOUND_RETRY_S
    assert poller.latest() is None
    assert any("HTTP 404" in r.message and r.levelname == "ERROR" for r in caplog.records)


async def test_wrong_password_backs_off(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    bridge = _Bridge()
    poller, client = _poller(bridge, password="wrong")
    delays = [await poller.poll_once(client) for _ in range(7)]
    assert delays[:5] == [4.0, 8.0, 16.0, 32.0, 60.0]
    assert delays[-1] == 60.0
    assert poller.latest() is None
    assert any("wrong bridge password" in r.message for r in caplog.records)


async def test_unreachable_bridge_backs_off_then_recovers(clock: _Clock) -> None:
    bridge = _Bridge()
    poller, client = _poller(bridge)
    await poller.poll_once(client)
    bridge.telegrams = [503, 503, VALID_2]
    assert await poller.poll_once(client) == 4.0
    assert await poller.poll_once(client) == 8.0
    assert poller.latest() is not None  # last good reading kept while fresh
    assert await poller.poll_once(client) == 2.0
    assert poller.latest().power_w == 604.0
    assert poller.error_streak == 0


async def test_bad_packets_keep_last_value_without_flapping(clock: _Clock) -> None:
    bridge = _Bridge()
    poller, client = _poller(bridge)
    await poller.poll_once(client)
    bridge.telegrams = [_crc_error(VALID_2), VALID_2[:300], b"", VALID_2]
    for _ in range(3):
        clock.now += 2
        assert await poller.poll_once(client) == 2.0
        reading = poller.latest()
        assert reading is not None and reading.power_w == 592.0
    assert poller.invalid_total == 3
    clock.now += 2
    await poller.poll_once(client)
    assert poller.latest().power_w == 604.0
    assert poller.invalid_streak == 0


async def test_only_bad_packets_go_unavailable_after_stale_timeout(clock: _Clock) -> None:
    bridge = _Bridge()
    poller, client = _poller(bridge)
    await poller.poll_once(client)
    bridge.telegrams = [_crc_error(VALID_2)]
    clock.now += 29
    await poller.poll_once(client)
    assert poller.latest() is not None
    clock.now += 2
    await poller.poll_once(client)
    assert poller.latest() is None


async def test_repeated_identical_telegram_does_not_renew_freshness(clock: _Clock) -> None:
    bridge = _Bridge()
    poller, client = _poller(bridge)
    await poller.poll_once(client)
    first = poller.latest()
    clock.now += 20
    await poller.poll_once(client)  # bridge still serves the same telegram
    assert poller.latest() is first
    clock.now += 11
    await poller.poll_once(client)
    assert poller.latest() is None


async def test_node_without_meter_data_is_unavailable(clock: _Clock) -> None:
    bridge = _Bridge()
    bridge.nodes = [{"node_id": 1, "available": True, "last_data_ms": 120_000}]
    poller, client = _poller(bridge)
    await poller.poll_once(client)
    assert poller.latest() is None

    bridge.nodes = [{"node_id": 1, "available": False, "last_data_ms": 500}]
    clock.now += bridge_mod._NODE_INFO_INTERVAL_S
    await poller.poll_once(client)
    assert poller.latest() is None

    bridge.nodes = [{"node_id": 1, "available": True, "last_data_ms": 500}]
    bridge.telegrams = [VALID_2]
    clock.now += bridge_mod._NODE_INFO_INTERVAL_S
    await poller.poll_once(client)
    assert poller.latest() is not None


async def test_node_info_failure_is_not_fatal(clock: _Clock) -> None:
    bridge = _Bridge()
    bridge.nodes = 500
    poller, client = _poller(bridge)
    await poller.poll_once(client)
    assert poller.node is not None  # node_metrics still answered
    assert poller.latest() is not None


async def test_background_task_polls(clock: _Clock) -> None:
    bridge = _Bridge()
    poller = PulseBridgePoller(
        "http://192.0.2.10/", "secret", poll_interval_s=0.5, transport=httpx.MockTransport(bridge)
    )
    poller.ensure_started()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if poller.latest() is not None:
            break
    await poller.stop()
    assert poller.latest() is not None
    assert bridge.requests[0].startswith("/nodes.json")


# ---------------------------------------------------------------------------
# device


class _StubPoller:
    def __init__(self, reading=None, node=None) -> None:
        self.reading = reading
        self.node = node
        self.started = 0

    def ensure_started(self) -> None:
        self.started += 1

    def latest(self):
        return self.reading


class _StubFallback:
    def __init__(self, state: DeviceState) -> None:
        self.state = state
        self.calls = 0

    async def get_state(self) -> DeviceState:
        self.calls += 1
        return self.state


async def test_device_reports_local_reading() -> None:
    reading = decode_telegram(_with_power(VALID, -300), received_at=TS)
    node = bridge_mod.NodeInfo(fetched_mono=0.0, battery_voltage=2.69)
    poller = _StubPoller(reading, node)
    dev = TibberLocalDevice("household_meter", DeviceRole.METER, poller)  # type: ignore[arg-type]
    state = await dev.get_state()
    assert poller.started == 1
    assert state.available and state.timestamp == TS
    assert state.power_w == -300.0
    assert state.energy_kwh == pytest.approx(11076.5928)
    assert state.extra["source"] == "local"
    assert state.extra["import_w"] == 0.0 and state.extra["export_w"] == 300.0
    assert state.extra["meter_production_kwh"] == pytest.approx(2766.0098)
    assert state.extra["l1_power_w"] == 277.0
    assert state.extra["pulse_battery_v"] == 2.69


async def test_device_unavailable_without_reading() -> None:
    dev = TibberLocalDevice("m", DeviceRole.METER, _StubPoller())  # type: ignore[arg-type]
    state = await dev.get_state()
    assert not state.available and state.power_w is None


async def test_device_uses_cloud_fallback_only_without_local_reading() -> None:
    cloud = _StubFallback(DeviceState(device_id="m", power_w=480.0, available=True, extra={"import_w": 480.0}))
    poller = _StubPoller()
    dev = TibberLocalDevice("m", DeviceRole.METER, poller, fallback=cloud)  # type: ignore[arg-type]

    state = await dev.get_state()
    assert state.power_w == 480.0 and state.extra["source"] == "cloud"

    poller.reading = decode_telegram(VALID)
    state = await dev.get_state()
    assert state.power_w == 592.0 and state.extra["source"] == "local"
    assert cloud.calls == 2  # kept warm


async def test_device_unavailable_when_both_sources_down() -> None:
    cloud = _StubFallback(DeviceState(device_id="m", power_w=None, available=False))
    dev = TibberLocalDevice("m", DeviceRole.METER, _StubPoller(), fallback=cloud)  # type: ignore[arg-type]
    assert not (await dev.get_state()).available


# ---------------------------------------------------------------------------
# registration


def _ctx() -> BuildContext:
    return BuildContext(backends=None)  # type: ignore[arg-type]


def test_registered_and_built_from_config() -> None:
    dev = registry.build_device(
        "household_meter",
        {"type": "tibber_local", "role": "meter", "host": "192.0.2.10", "password": "x"},
        _ctx(),
    )
    assert isinstance(dev, TibberLocalDevice)
    assert dev.role == DeviceRole.METER
    assert dev._fallback is None
    assert dev._poller._base_url == "http://192.0.2.10"
    assert dev._poller._node_id == 1


def test_config_with_token_adds_cloud_fallback() -> None:
    dev = registry.build_device(
        "household_meter",
        {"type": "tibber_local", "host": "bridge.lan", "password": "x", "token": "tok", "node_id": 2, "poll_interval_s": 1},
        _ctx(),
    )
    assert dev._fallback is not None
    assert dev._poller._node_id == 2
    assert dev._poller._poll_interval_s == 1.0


@pytest.mark.parametrize("cfg", [{"host": "bridge.lan"}, {"password": "x"}])
def test_missing_host_or_password_skips_device(cfg: dict) -> None:
    assert registry.build_device("m", {"type": "tibber_local", **cfg}, _ctx()) is None


def test_config_api_url_reaches_cloud_fallback() -> None:
    dev = registry.build_device(
        "household_meter",
        {"type": "tibber_local", "host": "bridge.lan", "password": "x", "token": "tok-url",
         "api_url": "http://sim:8000/tibber/gql"},
        _ctx(),
    )
    assert dev._fallback._stream._api._api_url == "http://sim:8000/tibber/gql"

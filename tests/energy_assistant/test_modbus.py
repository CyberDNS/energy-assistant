"""Tests for the native Modbus plugin: codec, profiles, device and TCP transport."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

import pytest

from energy_assistant.core.models import DeviceCommand, DeviceRole
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.core.config import BackendsConfig
from energy_assistant.plugins import registry as plugin_registry
from energy_assistant.plugins._modbus import codec
from energy_assistant.plugins._modbus.client import (
    ModbusConnectionPool,
    ModbusError,
    ModbusTcpClient,
)
from energy_assistant.plugins.modbus.control import ControlConfigError, build_strategy
from energy_assistant.plugins.modbus.device import ModbusDevice
from energy_assistant.plugins.modbus.profile import (
    ProfileError,
    available_profiles,
    load_profile,
    parse_profile,
)
from helpers.fake_modbus_client import FakeModbusClient

SMA = "sma_sunny_boy_storage"


# ── codec ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("type_name", "value", "words"),
    [
        ("u16", 65535, [0xFFFF]),
        ("s16", -1, [0xFFFF]),
        ("s16", 100, [100]),
        ("u32", 70000, [0x0001, 0x1170]),
        ("s32", -2500, [0xFFFF, 0xF63C]),
        ("s64", -1, [0xFFFF] * 4),
        ("u64", 2**32, [0, 1, 0, 0]),
        ("f32", 1.5, [0x3FC0, 0x0000]),
    ],
)
def test_codec_roundtrip(type_name: str, value: float, words: list[int]) -> None:
    assert codec.encode(value, type_name) == words
    assert codec.decode(words, type_name) == value


def test_codec_little_word_order() -> None:
    assert codec.encode(70000, "u32", "little") == [0x1170, 0x0001]
    assert codec.decode([0x1170, 0x0001], "u32", "little") == 70000
    assert codec.raw_unsigned([0x1170, 0x0001], "little") == 70000


def test_codec_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        codec.decode([1], "u32")
    with pytest.raises(ValueError):
        codec.encode(70000, "s16")


def test_plan_reads_merges_only_adjacent_spans() -> None:
    blocks = codec.plan_reads(
        [
            ("holding", 40191, 2),
            ("input", 30775, 2),
            ("holding", 40189, 2),
            ("input", 30845, 2),
            ("input", 30775, 2),  # duplicate
        ]
    )
    assert blocks == [
        codec.ReadBlock("holding", 40189, 4),
        codec.ReadBlock("input", 30775, 2),
        codec.ReadBlock("input", 30845, 2),
    ]


def test_plan_reads_respects_request_size_limit() -> None:
    blocks = codec.plan_reads(("input", addr, 2) for addr in range(0, 200, 2))
    assert [b.count for b in blocks] == [124, 76]
    assert all(b.count <= codec.MAX_READ_COUNT for b in blocks)


# ── profiles ─────────────────────────────────────────────────────────────────


def test_bundled_profiles_all_load() -> None:
    assert SMA in available_profiles()
    for name in available_profiles():
        load_profile(name)


def test_sma_profile_contents() -> None:
    profile = load_profile(SMA)
    assert (profile.port, profile.unit_id, profile.word_order) == (502, 3, "big")
    power = profile.registers["power_ac"]
    assert (power.table, power.address, power.type, power.count) == ("input", 30775, "s32", 2)
    assert profile.state["power_w"].scale == -1
    # The write-only limit register is declared but must not be polled.
    assert "active_power_limit" in profile.registers
    assert "active_power_limit" not in {r.name for r in profile.polled_registers()}


def test_load_profile_from_path(tmp_path: Path) -> None:
    path = tmp_path / "mine.yaml"
    path.write_text(
        "registers:\n  p: {table: holding, address: 10, type: s16, scale: 10}\n"
        "state:\n  power_w: p\n"
    )
    profile = load_profile(str(path))
    assert profile.name == "mine"
    assert profile.registers["p"].scale == 10.0
    assert profile.unit_id == 1


def test_load_profile_unknown_name_lists_bundled() -> None:
    with pytest.raises(ProfileError, match=SMA):
        load_profile("does_not_exist")


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({}, "registers"),
        ({"registers": {"p": {"table": "coil", "address": 1, "type": "s16"}}}, "table"),
        ({"registers": {"p": {"table": "input", "address": 1, "type": "s24"}}}, "unknown type"),
        ({"registers": {"p": {"table": "input", "address": "1", "type": "s16"}}}, "integer"),
        ({"registers": {"p": {"table": "input", "address": 1}}}, "missing 'type'"),
        (
            {"registers": {"p": {"table": "input", "address": 1, "type": "s16", "sacle": 2}}},
            "unknown key",
        ),
        (
            {
                "registers": {"p": {"table": "input", "address": 1, "type": "s16"}},
                "state": {"power_w": "q"},
            },
            "unknown register",
        ),
        (
            {
                "registers": {"p": {"table": "input", "address": 1, "type": "s16"}},
                "state": {"power": "p"},
            },
            "unknown key",
        ),
        (
            {
                "registers": {"p": {"table": "input", "address": 1, "type": "s16"}},
                "control": {"strategy": "x"},
            },
            "control.strategy 'x' is unknown",
        ),
        (
            {
                "registers": {"p": {"table": "input", "address": 1, "type": "s16"}},
                "control": {"strategy": "discharge_limit_pct", "register": "p"},
            },
            "not a holding register",
        ),
        (
            {
                "registers": {"p": {"table": "holding", "address": 1, "type": "s16"}},
                "control": {"strategy": "discharge_limit_pct"},
            },
            "must name a declared register",
        ),
    ],
)
def test_parse_profile_rejects_malformed(doc: dict, message: str) -> None:
    with pytest.raises(ProfileError, match=message):
        parse_profile(doc)


# ── device (fake transport) ──────────────────────────────────────────────────


def _sma_client(
    power_ac: int = 1200, soc: int = 57, max_cha: int = 2500, max_dsch: int = 2500
) -> FakeModbusClient:
    client = FakeModbusClient()
    client.set("input", 30775, codec.encode(power_ac, "s32"))
    client.set("input", 30845, codec.encode(soc, "u32"))
    client.set("holding", 40189, codec.encode(max_cha, "u32"))
    client.set("holding", 40191, codec.encode(max_dsch, "u32"))
    return client


def _sma_device(client: FakeModbusClient, **kwargs) -> ModbusDevice:
    return ModbusDevice("sma_battery", client, load_profile(SMA), **kwargs)


async def test_device_reads_sma_state_with_platform_sign() -> None:
    client = _sma_client(power_ac=1200, soc=57)  # SMA: positive = discharging
    state = await _sma_device(client).get_state()

    assert state.available is True
    assert state.power_w == -1200.0
    assert state.soc_pct == 57.0
    assert state.extra == {"max_cha_w": 2500.0, "max_dsch_w": 2500.0}


async def test_device_charging_is_positive() -> None:
    state = await _sma_device(_sma_client(power_ac=-800)).get_state()
    assert state.power_w == 800.0


async def test_device_batches_adjacent_registers_and_uses_profile_unit_id() -> None:
    client = _sma_client()
    await _sma_device(client).get_state()
    assert client.reads == [
        ("holding", 40189, 4, 3),
        ("input", 30775, 2, 3),
        ("input", 30845, 2, 3),
    ]


async def test_device_unit_id_override() -> None:
    client = _sma_client()
    await _sma_device(client, unit_id=7).get_state()
    assert {read[3] for read in client.reads} == {7}


async def test_device_maps_nan_sentinel_to_none() -> None:
    client = _sma_client()
    client.set("input", 30775, [0x8000, 0x0000])  # SMA s32 "no value"
    client.set("input", 30845, [0xFFFF, 0xFFFF])  # SMA u32 "no value"
    state = await _sma_device(client).get_state()

    assert state.available is True
    assert state.power_w is None
    assert state.soc_pct is None
    assert state.extra["max_cha_w"] == 2500.0


async def test_device_partial_failure_keeps_other_values() -> None:
    client = _sma_client()
    del client.registers[("holding", 40189)]  # device rejects this block
    state = await _sma_device(client).get_state()

    assert state.available is True
    assert state.power_w == -1200.0
    assert state.extra == {"max_cha_w": None, "max_dsch_w": None}


async def test_device_unavailable_when_nothing_can_be_read(caplog) -> None:
    client = _sma_client()
    client.offline = True
    device = _sma_device(client)

    with caplog.at_level(logging.WARNING):
        first = await device.get_state()
        await device.get_state()
    assert first.available is False
    assert first.power_w is None
    # Logged once, not on every poll.
    assert len([r for r in caplog.records if "unreachable" in r.message]) == 1

    client.offline = False
    assert (await device.get_state()).available is True


async def test_device_applies_register_and_state_scale() -> None:
    profile = parse_profile(
        {
            "registers": {
                "e": {"table": "input", "address": 0, "type": "u32", "scale": 0.1},
                "p": {"table": "input", "address": 2, "type": "s16"},
            },
            "state": {"energy_kwh": {"register": "e", "scale": 0.001}, "power_w": "p"},
        }
    )
    client = FakeModbusClient()
    client.set("input", 0, codec.encode(1234500, "u32") + codec.encode(-40, "s16"))
    device = ModbusDevice("meter", client, profile, role=DeviceRole.METER)

    state = await device.get_state()
    assert client.reads == [("input", 0, 3, 1)]
    assert state.energy_kwh == pytest.approx(123.45)
    assert state.power_w == -40.0
    assert device.role is DeviceRole.METER


async def test_device_without_strategy_is_read_only() -> None:
    client = _sma_client()
    device = _sma_device(client)
    await device.send_command(
        DeviceCommand(device_id="sma_battery", command="set_power_w", value=-1000)
    )
    assert client.written == []
    assert device.controls_charge is False


# ── control ──────────────────────────────────────────────────────────────────


def _controlled_sma(client: FakeModbusClient, **cfg) -> ModbusDevice:
    profile = load_profile(SMA)
    strategy = build_strategy(
        profile.control.strategy, profile.control.registers, {"max_discharge_kw": 2.5, **cfg}
    )
    return ModbusDevice("sma_battery", client, profile, strategy=strategy)


def _set_power(value: float) -> DeviceCommand:
    return DeviceCommand(device_id="sma_battery", command="set_power_w", value=value)


@pytest.mark.parametrize(
    ("power_w", "pct"),
    [
        (-2750.0, 100),  # full scale = 2500 W × 253/230
        (-1375.0, 50),
        (-5000.0, 100),  # clamped
        (-10.0, 0),      # rounds down to nothing
        (0.0, 0),
        (1500.0, 0),     # charge request: inverter charges on its own
    ],
)
async def test_sma_discharge_limit_written_as_s16_percent(power_w: float, pct: int) -> None:
    client = _sma_client()
    device = _controlled_sma(client)

    await device.send_command(_set_power(power_w))

    assert client.written == [(40016, [pct], 3)]
    assert device.read_only is False
    assert device.controls_charge is False


async def test_sma_control_matches_iobroker_plugin() -> None:
    """Same setpoints must yield the same percentage as sma_modbus_iobroker."""
    from energy_assistant.plugins.sma_modbus_iobroker.device import SmaSunnyBoyStorageDevice
    from helpers.fake_iobroker_client import FakeIoBrokerClient

    iob = FakeIoBrokerClient()
    old = SmaSunnyBoyStorageDevice(
        "sma_battery", iob, capacity_kwh=7.4, max_charge_kw=3.68, max_discharge_kw=3.9,
        voltage_max_v=250.0,
    )
    client = _sma_client()
    new = _controlled_sma(client, max_discharge_kw=3.9, voltage_max_v=250.0)

    for power_w in (-4500, -3900, -2000, -777, -1, 0, 500):
        await old.send_command(_set_power(power_w))
        await new.send_command(_set_power(power_w))

    assert [words[0] for _, words, _ in client.written] == [value for _, value in iob.written]


async def test_control_writes_every_command_and_logs_only_changes(caplog) -> None:
    client = _sma_client()
    device = _controlled_sma(client)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            await device.send_command(_set_power(-1375))
    assert len(client.written) == 3
    assert len([r for r in caplog.records if "active_power_limit = 50" in r.message]) == 1


async def test_control_rejects_unsupported_command() -> None:
    client = _sma_client()
    with pytest.raises(NotImplementedError):
        await _controlled_sma(client).send_command(
            DeviceCommand(device_id="sma_battery", command="turn_on")
        )
    assert client.written == []


async def test_control_write_failure_propagates() -> None:
    client = _sma_client()
    client.offline = True
    with pytest.raises(ModbusError):
        await _controlled_sma(client).send_command(_set_power(-1000))


def test_strategy_requires_max_discharge() -> None:
    with pytest.raises(ControlConfigError, match="max_discharge_kw"):
        build_strategy("discharge_limit_pct", {"register": "active_power_limit"}, {})


# ── plugin factory ───────────────────────────────────────────────────────────


def _build(cfg: dict, ctx: BuildContext | None = None):
    ctx = ctx or BuildContext(backends=BackendsConfig())
    return plugin_registry.build_device("sma_battery", {"type": "modbus", **cfg}, ctx), ctx


def test_factory_builds_device_and_shares_connection() -> None:
    cfg = {"role": "storage", "profile": SMA, "host": "192.0.2.10"}
    device, ctx = _build(cfg)
    assert isinstance(device, ModbusDevice)
    assert device.role is DeviceRole.STORAGE
    assert device.profile.name == "SMA Sunny Boy Storage"

    other, _ = _build({**cfg, "unit_id": 4}, ctx)
    assert other._client is device._client
    assert (device._unit_id, other._unit_id) == (3, 4)
    assert isinstance(ctx.modbus_pool, ModbusConnectionPool)


def test_factory_enables_control_only_when_configured() -> None:
    base = {"role": "storage", "profile": SMA, "host": "192.0.2.10"}
    assert _build({**base, "max_discharge_kw": 3.9})[0].read_only is False
    assert _build({**base, "max_discharge_kw": 3.9, "read_only": True})[0].read_only is True
    # Strategy cannot be configured -> device still built, but read-only.
    assert _build(base)[0].read_only is True


@pytest.mark.parametrize(
    "cfg",
    [
        {"profile": SMA},
        {"host": "192.0.2.10"},
        {"host": "192.0.2.10", "profile": "does_not_exist"},
    ],
)
def test_factory_skips_incomplete_config(cfg: dict) -> None:
    device, _ = _build(cfg)
    assert device is None


# ── TCP transport against a real in-process Modbus server ────────────────────


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def modbus_server():
    from pymodbus.server import ModbusTcpServer
    from pymodbus.simulator import DataType, SimData, SimDevice

    def reg(address: int, words: list[int]) -> SimData:
        return SimData(address, values=words, datatype=DataType.REGISTERS)

    unit = SimDevice(
        id=3,
        simdata=[
            reg(30775, codec.encode(1200, "s32")),
            reg(30845, codec.encode(57, "u32")),
            reg(40016, [0]),
            reg(40189, codec.encode(2500, "u32") + codec.encode(2400, "u32")),
        ],
    )
    port = _free_port()
    server = ModbusTcpServer(unit, address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    for _ in range(200):
        if server.is_active():
            break
        await asyncio.sleep(0.01)
    try:
        yield port
    finally:
        await server.shutdown()
        task.cancel()


async def test_tcp_client_reads_and_writes(modbus_server: int) -> None:
    client = ModbusTcpClient("127.0.0.1", modbus_server, timeout=1.0)
    try:
        assert codec.decode(await client.read("input", 30775, 2, 3), "s32") == 1200
        assert await client.read("holding", 40189, 4, 3) == [0, 2500, 0, 2400]

        # Single-register write (function code 6).
        await client.write(40016, codec.encode(42, "s16"), 3)
        assert await client.read("holding", 40016, 1, 3) == [42]
        # Multi-register write (function code 16).
        await client.write(40189, codec.encode(3000, "u32"), 3)
        assert await client.read("holding", 40189, 2, 3) == [0, 3000]

        with pytest.raises(ModbusError, match="rejected"):
            await client.read("input", 100, 2, 3)  # address not implemented
        # The connection survives an exception response.
        assert await client.read("input", 30845, 2, 3) == [0, 57]
    finally:
        await client.close()


async def test_tcp_device_end_to_end_and_reconnect(modbus_server: int) -> None:
    client = ModbusTcpClient("127.0.0.1", modbus_server, timeout=1.0)
    device = ModbusDevice("sma_battery", client, load_profile(SMA))
    try:
        state = await device.get_state()
        assert (state.available, state.power_w, state.soc_pct) == (True, -1200.0, 57.0)
        assert state.extra == {"max_cha_w": 2500.0, "max_dsch_w": 2400.0}

        await client.close()  # connection drops
        assert (await device.get_state()).power_w == -1200.0  # reopened lazily
    finally:
        await client.close()


async def test_tcp_client_unreachable_host() -> None:
    client = ModbusTcpClient("127.0.0.1", _free_port(), timeout=0.5)
    with pytest.raises(ModbusError, match="cannot connect"):
        await client.read("input", 30775, 2, 3)
    device = ModbusDevice("sma_battery", client, load_profile(SMA))
    assert (await device.get_state()).available is False

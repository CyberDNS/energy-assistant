"""Tests for the native Modbus plugin: codec, profiles, device and TCP transport."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers.fake_modbus_client import FakeModbusClient

from nornwatt.core.config import BackendsConfig
from nornwatt.core.device_model import DeviceCategory
from nornwatt.core.models import DeviceCommand
from nornwatt.core.plugin_registry import BuildContext
from nornwatt.plugins import registry as plugin_registry
from nornwatt.plugins._modbus import codec
from nornwatt.plugins._modbus.client import (
    ModbusConnectionPool,
    ModbusError,
    ModbusTcpClient,
)
from nornwatt.plugins.modbus.control import ControlConfigError, build_strategy
from nornwatt.plugins.modbus.device import ModbusDevice
from nornwatt.plugins.modbus.profile import (
    ProfileError,
    available_profiles,
    load_profile,
    parse_profile,
)

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
    device = ModbusDevice("meter", client, profile, category=DeviceCategory.METER)

    state = await device.get_state()
    assert client.reads == [("input", 0, 3, 1)]
    assert state.energy_kwh == pytest.approx(123.45)
    assert state.power_w == -40.0
    assert device.category is DeviceCategory.METER


async def test_device_without_strategy_is_read_only() -> None:
    client = _sma_client()
    device = _sma_device(client)
    await device.send_command(
        DeviceCommand(device_id="sma_battery", command="set_power_w", value=-1000)
    )
    assert client.written == []
    assert device.controls_charge is False


# ── control ──────────────────────────────────────────────────────────────────


def _limit_sma(client: FakeModbusClient, **cfg) -> ModbusDevice:
    """The SMA with the former discharge-limit strategy (still available)."""
    profile = load_profile(SMA)
    strategy = build_strategy(
        "discharge_limit_pct", {"register": "active_power_limit"},
        {"max_discharge_kw": 2.5, **cfg},
    )
    return ModbusDevice("sma_battery", client, profile, strategy=strategy)


def _controlled_sma(client: FakeModbusClient, **cfg) -> ModbusDevice:
    """The SMA with its profile's strategy (battery management)."""
    profile = load_profile(SMA)
    strategy = build_strategy(
        profile.control.strategy, profile.control.registers,
        {"max_charge_kw": 3.0, "max_discharge_kw": 2.5, "discharge_efficiency": 1.0, **cfg},
    )
    return ModbusDevice("sma_battery", client, profile, strategy=strategy)


# Battery management registers of the SBS: mode, charge min/max, discharge
# min/max, grid setpoint, active power limit.
_BMS = (40236, 40793, 40795, 40797, 40799, 40801, 40016)


def _bms_values(client: FakeModbusClient) -> dict[int, int]:
    """Last value written per register (decoded)."""
    out: dict[int, int] = {}
    for address, words, _unit in client.written:
        out[address] = codec.decode(words, "s16" if len(words) == 1 else "s32")
    return out


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
    device = _limit_sma(client)

    await device.send_command(_set_power(power_w))

    assert client.written == [(40016, [pct], 3)]
    assert device.read_only is False
    assert device.controls_charge is False


async def test_sma_control_percentages() -> None:
    """Setpoint → active power limit (%), as the former sma_modbus_iobroker plugin wrote it."""
    client = _sma_client()
    device = _limit_sma(client, max_discharge_kw=3.9, voltage_max_v=250.0)
    for power_w in (-4500, -3900, -2000, -777, -1, 0, 500):
        await device.send_command(_set_power(power_w))
    assert [words[0] for _, words, _ in client.written] == [100, 92, 47, 18, 0, 0, 0]


async def test_control_writes_every_command_and_logs_only_changes(caplog) -> None:
    client = _sma_client()
    device = _limit_sma(client)
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


@pytest.mark.parametrize(
    ("power_w", "expected"),
    [
        # charge: charge mode, exactly P (rounded to 50 W, capped at 3 kW)
        (1234.0, (2289, 1250, 1250, 0, 0, 0, 100)),
        (5000.0, (2289, 3000, 3000, 0, 0, 0, 100)),
        # discharge: default mode, no charging, discharge up to |P|
        (-1000.0, (2424, 0, 0, 0, 1000, 0, 100)),
        (-9000.0, (2424, 0, 0, 0, 2500, 0, 100)),
        # idle, and setpoints that round to nothing: hold
        (0.0, (2424, 0, 0, 0, 0, 0, 100)),
        (20.0, (2424, 0, 0, 0, 0, 0, 100)),
    ],
)
async def test_sma_battery_management_writes(power_w: float, expected: tuple) -> None:
    client = _sma_client()
    device = _controlled_sma(client)
    await device.send_command(_set_power(power_w))
    assert [a for a, _, _ in client.written] == list(_BMS)
    assert tuple(_bms_values(client)[a] for a in _BMS) == expected
    assert device.controls_charge is True


async def test_sma_battery_management_writes_only_changes_and_refreshes(monkeypatch) -> None:
    import nornwatt.plugins.modbus.device as device_mod

    now = [1000.0]
    monkeypatch.setattr(device_mod.time, "monotonic", lambda: now[0])
    client = _sma_client()
    device = _controlled_sma(client)
    for value in (1000.0, 1010.0, 990.0):   # all round to 1000 W
        await device.send_command(_set_power(value))
    assert len(client.written) == len(_BMS)
    await device.send_command(_set_power(1100.0))
    assert len(client.written) == 2 * len(_BMS)
    now[0] += 301.0   # unchanged, but due for a refresh
    await device.send_command(_set_power(1100.0))
    assert len(client.written) == 3 * len(_BMS)


async def test_sma_release_hands_back_self_consumption_and_rewrites_next_command() -> None:
    client = _sma_client()
    device = _controlled_sma(client)
    await device.send_command(_set_power(500.0))
    client.written.clear()
    await device.release()
    assert tuple(_bms_values(client)[a] for a in _BMS) == (2424, 0, 3000, 0, 2500, 0, 100)
    client.written.clear()
    await device.send_command(_set_power(500.0))   # same as before the release
    assert _bms_values(client)[40236] == 2289


async def test_failed_write_is_retried_next_tick() -> None:
    client = _sma_client()
    device = _controlled_sma(client)
    client.offline = True
    with pytest.raises(ModbusError):
        await device.send_command(_set_power(500.0))
    client.offline = False
    await device.send_command(_set_power(500.0))
    assert _bms_values(client)[40236] == 2289


async def test_discharge_limit_release_lifts_the_limit() -> None:
    client = _sma_client()
    await _limit_sma(client).release()
    assert client.written == [(40016, [100], 3)]


async def test_release_without_a_strategy_does_nothing() -> None:
    client = _sma_client()
    await _sma_device(client).release()
    assert client.written == []


def test_battery_management_requires_both_power_limits() -> None:
    profile = load_profile(SMA)
    with pytest.raises(ControlConfigError, match="max_charge_kw"):
        build_strategy(profile.control.strategy, profile.control.registers, {"max_discharge_kw": 3})


# ── plugin factory ───────────────────────────────────────────────────────────


def _build(cfg: dict, ctx: BuildContext | None = None):
    ctx = ctx or BuildContext(backends=BackendsConfig())
    return plugin_registry.build_device("sma_battery", {"type": "modbus", **cfg}, ctx), ctx


def test_factory_builds_device_and_shares_connection() -> None:
    cfg = {"category": "battery", "profile": SMA, "host": "192.0.2.10"}
    device, ctx = _build(cfg)
    assert isinstance(device, ModbusDevice)
    assert device.category is DeviceCategory.BATTERY
    assert device.profile.name == "SMA Sunny Boy Storage"

    other, _ = _build({**cfg, "unit_id": 4}, ctx)
    assert other._client is device._client
    assert (device._unit_id, other._unit_id) == (3, 4)
    assert isinstance(ctx.modbus_pool, ModbusConnectionPool)


def test_factory_enables_control_only_when_configured() -> None:
    base = {"category": "battery", "profile": SMA, "host": "192.0.2.10"}
    limits = {"max_charge_kw": 3.0, "max_discharge_kw": 3.9}
    assert _build({**base, **limits})[0].read_only is False
    assert _build({**base, **limits, "read_only": True})[0].read_only is True
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


# ── Application: release at start, reload and stop ───────────────────────────


class _ReleasingDevice:
    device_id = "sma_battery"

    def __init__(self, fail: bool = False) -> None:
        self.released = 0
        self.fail = fail

    async def release(self) -> None:
        self.released += 1
        if self.fail:
            raise ModbusError("offline")


def _app_with(devices: list, dry_run: bool):
    from nornwatt.server import Application

    app = Application.__new__(Application)
    app._dry_run = dry_run
    app._registry = SimpleNamespace(all=lambda: devices)
    return app


async def test_application_releases_devices_but_not_in_dry_run(caplog) -> None:
    ok, failing = _ReleasingDevice(), _ReleasingDevice(fail=True)
    await _app_with([failing, ok, SimpleNamespace(device_id="meter")], dry_run=False)._release_devices()
    assert (failing.released, ok.released) == (1, 1)
    assert "Could not release device" in caplog.text

    dry = _ReleasingDevice()
    await _app_with([dry], dry_run=True)._release_devices()
    assert dry.released == 0


@pytest.mark.parametrize(
    ("power_w", "offset", "dsch_max"),
    [
        (-57.0, 0.0, 100),     # rounded up, not to the nearest step (50)
        (-57.0, 30.0, 100),
        (-100.0, 0.0, 100),    # already on a step
        (-100.0, 30.0, 150),   # + the inverter's own draw
        (-131.0, 30.0, 200),
        (-2480.0, 30.0, 2500), # capped at max discharge
        (-20.0, 30.0, 0),      # below half a step: hold
    ],
)
async def test_sma_deprecated_discharge_offset_still_applies(
    power_w: float, offset: float, dsch_max: int
) -> None:
    """``bms_discharge_offset_w`` (before the battery model): |P| + offset,
    rounded up."""
    client = _sma_client()
    device = _controlled_sma(client, bms_discharge_offset_w=offset)
    await device.send_command(_set_power(power_w))
    assert _bms_values(client)[40799] == dsch_max


def test_sma_discharge_offset_must_not_be_negative() -> None:
    profile = load_profile(SMA)
    with pytest.raises(ControlConfigError, match="bms_discharge_offset_w"):
        build_strategy(profile.control.strategy, profile.control.registers,
                       {"max_charge_kw": 3.0, "max_discharge_kw": 2.5, "bms_discharge_offset_w": -5})


def test_sma_bms_settings_pass_config_validation() -> None:
    from nornwatt.core.device_model import parse_devices
    from nornwatt.loader.device_loader import validate_plugins

    devices = parse_devices({"sma_battery": {
        "category": "battery", "type": "modbus", "profile": SMA, "host": "192.0.2.10",
        "bms_step_w": 50, "bms_discharge_offset_w": 30,
        "params": {"capacity_kwh": 7.4, "max_charge_kw": 3.0, "max_discharge_kw": 3.0},
    }})
    validate_plugins(devices)   # raises on unknown settings
    device, _ = _build({"category": "battery", "profile": SMA, "host": "192.0.2.10",
                        "max_charge_kw": 3.0, "max_discharge_kw": 3.0,
                        "bms_discharge_offset_w": 30})
    assert device._strategy._dsch_offset_w == 30.0


@pytest.mark.parametrize(
    ("power_w", "eta", "self_w", "dsch_max"),
    [
        (-721.0, 0.955, 25.0, 800),   # 755 + 25 = 780 → 800; AC ≈ (800 − 25) × 0.955 = 740
        (-57.0, 0.955, 25.0, 100),    # 60 + 25 = 85 → 100
        (-100.0, 1.0, 0.0, 100),      # lossless: the setpoint itself
        (-2480.0, 0.9, 25.0, 2500),   # capped at max discharge
        (-20.0, 0.9, 25.0, 0),        # below half a step: hold
    ],
)
async def test_sma_discharge_limit_from_the_battery_model(
    power_w: float, eta: float, self_w: float, dsch_max: int
) -> None:
    """The limit is battery-side: |P| / η_discharge + self_consumption_w, rounded up."""
    client = _sma_client()
    device = _controlled_sma(client, discharge_efficiency=eta, self_consumption_w=self_w)
    await device.send_command(_set_power(power_w))
    assert _bms_values(client)[40799] == dsch_max


async def test_sma_charge_is_not_translated() -> None:
    client = _sma_client()
    device = _controlled_sma(client, discharge_efficiency=0.9, self_consumption_w=25.0)
    await device.send_command(_set_power(1000.0))
    values = _bms_values(client)
    assert (values[40793], values[40795]) == (1000, 1000)

"""Tests for the tasmota plugin (Tasmota devices read over HTTP on the LAN).

``fixtures/tasmota/status10_sml_mt631.json`` is a ``Status 10`` answer recorded
from a Hichi IR head on an ISKRA MT631 (Tasmota 14.6.0.2).  The plug payloads
follow the standard Tasmota ``ENERGY`` sensor format; the reboot and partial
payloads are derived from the recorded one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from energy_assistant.core.device_model import DeviceCategory
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.plugins import registry
from energy_assistant.plugins.tasmota import poller as poller_mod
from energy_assistant.plugins.tasmota.device import TasmotaDevice
from energy_assistant.plugins.tasmota.mapping import (
    MappingError,
    PayloadError,
    build_field_map,
    map_payload,
    suggest_power_paths,
)
from energy_assistant.plugins.tasmota.poller import TasmotaPoller
from energy_assistant.plugins.tasmota.profile import (
    ProfileError,
    available_profiles,
    load_profile,
    matching_profiles,
    parse_profile,
)

DATA = Path(__file__).parent / "fixtures" / "tasmota"


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text())


SML = _load("status10_sml_mt631.json")
SML_REBOOT = _load("status10_sml_after_reboot.json")
SML_PARTIAL = _load("status10_sml_partial.json")
PLUG = _load("status10_energy_plug.json")
PLUG_2CH = _load("status10_energy_plug_2ch.json")
NEED_PASSWORD = _load("need_password.json")

METER_FIELDS = {
    "power_w": "MT631.P",
    "energy_in_kwh": "MT631.E_in",
    "energy_out_kwh": "MT631.E_out",
    "phase_power_w": ["MT631.L1", "MT631.L2", "MT631.L3"],
}
TS = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)


def _profile_map(name: str, fields: dict | None = None, **kw) -> object:
    p = load_profile(name)
    return build_field_map(fields, base=p.fields, sensor=p.sensor, auto_sensor=True, **kw)


def _sml(power: float, e_in: float = 18941.058) -> dict:
    return {"StatusSNS": {"Time": "x", "MT631": {**SML["StatusSNS"]["MT631"], "P": power, "E_in": e_in}}}


# ---------------------------------------------------------------------------
# field mapping


def test_maps_recorded_sml_payload() -> None:
    r = map_payload(SML, build_field_map(METER_FIELDS), received_at=TS)
    assert r.timestamp == TS  # Tasmota's local "Time" is ignored
    assert r.power_w == 601.0
    assert r.energy_in_kwh == pytest.approx(18941.058)
    assert r.energy_out_kwh == pytest.approx(2485.291)
    assert r.values == {"l1_power_w": 324.0, "l2_power_w": 90.0, "l3_power_w": 186.0}


def test_receive_time_defaults_to_utc_now() -> None:
    assert map_payload(SML, build_field_map(METER_FIELDS)).timestamp.tzinfo is UTC


def test_invert_sign_flips_power_and_phases() -> None:
    r = map_payload(SML, build_field_map(METER_FIELDS, invert_sign=True))
    assert r.power_w == -601.0
    assert r.values["l1_power_w"] == -324.0
    assert r.energy_in_kwh == pytest.approx(18941.058)  # counters keep their sign


def test_scale_and_dict_spec() -> None:
    fields = build_field_map({
        "power_w": {"path": "MT631.P", "scale": 0.001},
        "energy_in_kwh": {"path": "MT631.E_in", "scale": 1000},
    })
    r = map_payload(SML, fields)
    assert r.power_w == pytest.approx(0.601)
    assert r.energy_in_kwh == pytest.approx(18941058.0)


def test_energy_profile_for_plug() -> None:
    r = map_payload(PLUG, _profile_map("energy"))
    assert r.power_w == 123.0
    assert r.energy_in_kwh == pytest.approx(45.612)
    assert r.energy_out_kwh is None
    assert r.values == {"voltage_v": 231.0, "current_a": pytest.approx(0.567)}


def test_profile_fields_can_be_overridden_and_extended() -> None:
    fields = _profile_map("energy", {"current_a": None, "extra": {"power_factor": "Factor", "today_kwh": "Today"}})
    r = map_payload(PLUG, fields)
    assert "current_a" not in r.values
    assert r.values["power_factor"] == pytest.approx(0.94)
    assert r.values["today_kwh"] == pytest.approx(0.512)


def test_two_channel_plug_sums_power_and_counters() -> None:
    r = map_payload(PLUG_2CH, _profile_map("energy"))
    assert r.power_w == 55.0
    assert r.energy_in_kwh == pytest.approx(15.75)
    assert r.values == {"voltage_v": 230.0}  # a list current is not summed


def test_list_index_in_path() -> None:
    r = map_payload(PLUG_2CH, build_field_map({"power_w": "ENERGY.Power.1", "current_a": "ENERGY.Current.1"}))
    assert r.power_w == 15.0
    assert r.values["current_a"] == pytest.approx(0.065)


def test_numeric_strings_are_accepted() -> None:
    payload = {"StatusSNS": {"S": {"P": "512.5"}}}
    assert map_payload(payload, build_field_map({"power_w": "S.P"})).power_w == 512.5


def test_partial_payload_skips_missing_and_non_numeric_fields() -> None:
    fields = build_field_map({**METER_FIELDS, "power_w": "MT631.L2"})
    r = map_payload(SML_PARTIAL, fields)
    assert r.power_w == 90.0
    assert r.values == {"l2_power_w": 90.0}  # L1 "nan" and missing L3 left out


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(SML_PARTIAL, id="power-missing"),
        pytest.param({"StatusSNS": {"MT631": {"P": "nan"}}}, id="nan-string"),
        pytest.param({"StatusSNS": {"MT631": {"P": None}}}, id="null"),
        pytest.param({"StatusSNS": {"MT631": {"P": True}}}, id="bool"),
        pytest.param({"StatusSNS": {"MT631": 12}}, id="not-an-object"),
        pytest.param({"StatusSNS": "x"}, id="statussns-not-object"),
        pytest.param({"Command": "Unknown"}, id="unknown-command"),
        pytest.param(NEED_PASSWORD, id="need-password"),
        pytest.param([1, 2], id="not-a-dict"),
    ],
)
def test_unusable_payload_raises(payload: object) -> None:
    with pytest.raises(PayloadError):
        map_payload(payload, build_field_map(METER_FIELDS))


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({}, "power_w"),
        ({"power": "X.P"}, "unknown field"),
        ({"power_w": "X.P", "phase_power_w": "X.L1"}, "list"),
        ({"power_w": {"path": "X.P", "scale": 0}}, "scale"),
        ({"power_w": {"path": "X.P", "scale": "kW"}}, "scale"),
        ({"power_w": ""}, "no path"),
        ({"power_w": 1670}, "quote it"),
        ("X.P", "mapping"),
    ],
)
def test_invalid_field_config(fields: object, match: str) -> None:
    with pytest.raises(MappingError, match=match):
        build_field_map(fields)


def test_suggest_power_paths() -> None:
    assert suggest_power_paths(SML) == ["MT631.P"]
    assert suggest_power_paths(PLUG_2CH) == ["ENERGY.Power"]
    assert suggest_power_paths(NEED_PASSWORD) == []


# ---------------------------------------------------------------------------
# profiles


@pytest.mark.parametrize("name", available_profiles())
def test_bundled_profiles_load(name: str) -> None:
    p = load_profile(name)
    assert p.sensor and p.fields["power_w"]


def test_mt175_profile_reads_renamed_sensor() -> None:
    # The recorded head runs the MT175 docs script but names its meter MT631.
    r = map_payload(SML, _profile_map("iskra_mt175"))
    assert r.sensor == "MT631"
    assert r.power_w == 601.0 and r.energy_out_kwh == pytest.approx(2485.291)
    assert r.values == {"l1_power_w": 324.0, "l2_power_w": 90.0, "l3_power_w": 186.0}


def test_explicit_sensor_must_match() -> None:
    p = load_profile("iskra_mt175")
    fields = build_field_map(base=p.fields, sensor="MT175")  # no auto_sensor
    with pytest.raises(PayloadError, match="no sensor 'MT175'"):
        map_payload(SML, fields)
    assert map_payload(SML, build_field_map(base=p.fields, sensor="MT631")).power_w == 601.0


def test_ambiguous_auto_sensor_asks_for_sensor() -> None:
    payload = {"StatusSNS": {"Haus": {"P": 1}, "Heizung": {"P": 2}}}
    with pytest.raises(PayloadError, match="set 'sensor'"):
        map_payload(payload, _profile_map("iskra_mt175"))


def test_profile_overrides_are_relative_to_sensor() -> None:
    r = map_payload(SML, _profile_map("iskra_mt175", {"power_w": {"path": "P", "scale": -1}, "phase_power_w": None}))
    assert r.power_w == -601.0 and r.values == {}


def test_ebz_profile_converts_wh_and_keeps_numeric_names() -> None:
    payload = {"StatusSNS": {"SM": {"1_8_0": 12345678.9, "2_8_0": 1000.0, "16_7_0": -250.5, "36_7_0": -80}}}
    r = map_payload(payload, _profile_map("ebz_dd3"))
    assert r.power_w == -250.5
    assert r.energy_in_kwh == pytest.approx(12345.6789)
    assert r.energy_out_kwh == pytest.approx(1.0)
    assert r.values == {"l1_power_w": -80.0}


def test_matching_profiles() -> None:
    assert matching_profiles(SML) == ["iskra_mt175"]
    assert matching_profiles(PLUG) == ["energy"]
    assert matching_profiles({"StatusSNS": {"MT681": {"Total_in": 1, "Total_out": 0, "Power_cur": 5}}}) == [
        "iskra_mt631"  # MT681 also maps the phases, which are missing here
    ]
    assert matching_profiles(NEED_PASSWORD) == []


def test_profile_from_file(tmp_path: Path) -> None:
    f = tmp_path / "my_meter.yaml"
    f.write_text('sensor: Haus\nfields:\n  power_w: Power_curr\n  energy_in_kwh: "1_8_0"\ninvert_sign: true\n')
    p = load_profile(str(f))
    assert p.key == "my_meter" and p.invert_sign
    r = map_payload({"StatusSNS": {"Haus": {"Power_curr": 10, "1_8_0": 5}}}, build_field_map(base=p.fields, sensor=p.sensor))
    assert r.power_w == 10.0 and r.energy_in_kwh == 5.0


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ([], "mapping"),
        ({"fields": {"power_w": "P"}}, "sensor"),
        ({"sensor": "X"}, "fields"),
        ({"sensor": "X", "fields": {"energy_in_kwh": "E"}}, "power_w"),
        ({"sensor": "X", "fields": {"power_w": "P"}, "defaults": {}}, "unknown key"),
        ({"sensor": "X", "fields": {"power_w": 1670}}, "quote it"),
    ],
)
def test_invalid_profile(raw: object, match: str) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile(raw)


def test_unknown_profile_lists_bundled() -> None:
    with pytest.raises(ProfileError, match="iskra_mt175"):
        load_profile("nope")


# ---------------------------------------------------------------------------
# poller


class _Device:
    """MockTransport handler emulating Tasmota's /cm endpoint."""

    def __init__(self, password: str | None = None) -> None:
        self.answers: list[object] = [SML]
        self.password = password
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != "/cm":
            return httpx.Response(404)
        if self.password and request.url.params.get("password") != self.password:
            return httpx.Response(200, json=NEED_PASSWORD)
        item = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(item, int):
            return httpx.Response(item)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return httpx.Response(200, text=item)
        return httpx.Response(200, json=item)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(poller_mod, "_monotonic", c)
    return c


def _poller(device: _Device, **kw) -> tuple[TasmotaPoller, httpx.AsyncClient]:
    kw.setdefault("poll_interval_s", 2)
    kw.setdefault("stale_after_s", 30)
    poller = TasmotaPoller("192.0.2.46", build_field_map(METER_FIELDS), **kw)
    return poller, httpx.AsyncClient(transport=httpx.MockTransport(device))


async def test_poll_reads_status10(clock: _Clock) -> None:
    device = _Device()
    poller, client = _poller(device)
    assert await poller.poll_once(client) == 2.0
    reading = poller.latest()
    assert reading is not None and reading.power_w == 601.0
    req = device.requests[0]
    assert req.url.params["cmnd"] == "Status 10"
    assert "password" not in req.url.params


async def test_web_password_is_sent(clock: _Clock) -> None:
    device = _Device(password="s3cret")
    poller, client = _poller(device, password="s3cret")
    await poller.poll_once(client)
    assert poller.latest() is not None
    params = device.requests[0].url.params
    assert params["user"] == "admin" and params["password"] == "s3cret"


async def test_missing_password_backs_off_with_hint(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    device = _Device(password="s3cret")
    poller, client = _poller(device)
    delays = [await poller.poll_once(client) for _ in range(6)]
    assert delays[:5] == [4.0, 8.0, 16.0, 32.0, 60.0]
    assert poller.latest() is None
    assert any("set 'password'" in r.message for r in caplog.records)


async def test_http_401_is_reported(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    device = _Device()
    device.answers = [401]
    poller, client = _poller(device)
    await poller.poll_once(client)
    assert any("wrong web password" in r.message for r in caplog.records)


async def test_timeouts_keep_last_value_then_recover(clock: _Clock) -> None:
    device = _Device()
    poller, client = _poller(device)
    await poller.poll_once(client)
    device.answers = [httpx.ConnectTimeout("timeout"), 503, "<html>busy</html>", _sml(-850)]
    assert await poller.poll_once(client) == 4.0
    assert await poller.poll_once(client) == 8.0
    assert await poller.poll_once(client) == 16.0  # not JSON
    assert poller.latest().power_w == 601.0  # last good value while fresh
    assert await poller.poll_once(client) == 2.0
    assert poller.latest().power_w == -850.0
    assert poller.error_streak == 0


async def test_bad_values_do_not_flap_and_go_stale_only_after_timeout(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    device = _Device()
    poller, client = _poller(device)
    await poller.poll_once(client)
    device.answers = [SML_PARTIAL]
    for _ in range(14):
        clock.now += 2
        assert await poller.poll_once(client) == 2.0
        assert poller.latest() is not None and poller.latest().power_w == 601.0
    clock.now += 4
    await poller.poll_once(client)
    assert poller.latest() is None
    assert poller.invalid_total == 15
    assert any("unavailable" in r.message for r in caplog.records)

    device.answers = [SML]
    await poller.poll_once(client)
    assert poller.latest() is not None and poller.invalid_streak == 0


async def test_wrong_power_path_logs_candidates(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    device = _Device()
    poller = TasmotaPoller("192.0.2.46", build_field_map({"power_w": "SML.Power"}))
    client = httpx.AsyncClient(transport=httpx.MockTransport(device))
    await poller.poll_once(client)
    assert poller.latest() is None
    msg = next(r.message for r in caplog.records if "power candidates" in r.message)
    assert "matching profiles: iskra_mt175" in msg and "MT631.P" in msg


async def test_renamed_sensor_is_logged_once(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    device = _Device()
    poller = TasmotaPoller("192.0.2.46", _profile_map("iskra_mt175"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(device))
    for _ in range(3):
        await poller.poll_once(client)
    assert poller.latest().power_w == 601.0
    assert sum("reading 'MT631' instead" in r.message for r in caplog.records) == 1


async def test_reboot_zeros_are_skipped(clock: _Clock) -> None:
    device = _Device()
    poller, client = _poller(device)
    await poller.poll_once(client)
    device.answers = [SML_REBOOT, SML_REBOOT, _sml(640)]
    for _ in range(2):
        clock.now += 2
        await poller.poll_once(client)
        assert poller.latest().power_w == 601.0
    clock.now += 2
    await poller.poll_once(client)
    assert poller.latest().power_w == 640.0


async def test_lasting_counter_reset_is_accepted(clock: _Clock) -> None:
    device = _Device()
    poller, client = _poller(device)
    await poller.poll_once(client)
    device.answers = [_sml(5, e_in=0.0)]
    clock.now += 2
    await poller.poll_once(client)
    assert poller.latest().power_w == 601.0
    clock.now += 15  # half of stale_after_s
    await poller.poll_once(client)
    assert poller.latest().power_w == 5.0 and poller.latest().energy_in_kwh == 0.0
    clock.now += 2
    await poller.poll_once(client)
    assert poller.latest() is not None


async def test_background_task_polls(clock: _Clock) -> None:
    device = _Device()
    poller = TasmotaPoller(
        "http://192.0.2.46/", build_field_map(METER_FIELDS),
        poll_interval_s=0.5, transport=httpx.MockTransport(device),
    )
    poller.ensure_started()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if poller.latest() is not None:
            break
    await poller.stop()
    assert poller.latest() is not None
    assert str(device.requests[0].url).startswith("http://192.0.2.46/cm?")


# ---------------------------------------------------------------------------
# device


class _StubPoller:
    def __init__(self, reading=None) -> None:
        self.reading = reading
        self.started = 0

    def ensure_started(self) -> None:
        self.started += 1

    def latest(self):
        return self.reading


@pytest.mark.parametrize("category", [DeviceCategory.METER])
async def test_meter_state(category: DeviceCategory) -> None:
    reading = map_payload(_sml(-300), build_field_map(METER_FIELDS), received_at=TS)
    poller = _StubPoller(reading)
    state = await TasmotaDevice("main_grid_meter", category, poller).get_state()  # type: ignore[arg-type]
    assert poller.started == 1
    assert state.available and state.timestamp == TS
    assert state.power_w == -300.0
    assert state.energy_kwh == pytest.approx(18941.058)
    assert state.extra["import_w"] == 0.0 and state.extra["export_w"] == 300.0
    assert state.extra["meter_production_kwh"] == pytest.approx(2485.291)
    assert state.extra["l3_power_w"] == 186.0
    assert state.extra["source"] == "tasmota"


async def test_consumer_state_has_no_import_export() -> None:
    reading = map_payload(PLUG, _profile_map("energy"))
    state = await TasmotaDevice("dishwasher", DeviceCategory.CONSUMER, _StubPoller(reading)).get_state()  # type: ignore[arg-type]
    assert state.power_w == 123.0 and state.energy_kwh == pytest.approx(45.612)
    assert "import_w" not in state.extra
    assert state.extra["voltage_v"] == 231.0


async def test_unavailable_without_reading() -> None:
    state = await TasmotaDevice("m", DeviceCategory.METER, _StubPoller()).get_state()  # type: ignore[arg-type]
    assert not state.available and state.power_w is None


# ---------------------------------------------------------------------------
# registration


def _ctx() -> BuildContext:
    return BuildContext(backends=None)  # type: ignore[arg-type]


def test_meter_built_from_config() -> None:
    dev = registry.build_device(
        "main_grid_meter",
        {"type": "tasmota", "category": "meter", "host": "192.0.2.46", "fields": METER_FIELDS},
        _ctx(),
    )
    assert isinstance(dev, TasmotaDevice)
    assert dev.category == DeviceCategory.METER
    assert dev._poller._base_url == "http://192.0.2.46"
    assert dev._poller._poll_interval_s == 2.0
    assert dev._poller._stale_after_s == 30.0
    assert "password" not in dev._poller._params


def test_grid_meter_polls_like_a_meter() -> None:
    dev = registry.build_device(
        "main_grid_meter",
        {"type": "tasmota", "category": "meter", "host": "h", "fields": METER_FIELDS},
        _ctx(),
    )
    assert dev.category == DeviceCategory.METER
    assert dev._poller._poll_interval_s == 2.0


def test_meter_built_from_profile_with_overrides() -> None:
    dev = registry.build_device(
        "main_grid_meter",
        {"type": "tasmota", "category": "meter", "host": "h", "profile": "iskra_mt175", "fields": {"phase_power_w": None}},
        _ctx(),
    )
    f = dev._poller._fields
    assert f.sensor == "MT175" and f.auto_sensor and not f.phases
    assert map_payload(SML, f).power_w == 601.0


def test_config_sensor_disables_auto_sensor() -> None:
    dev = registry.build_device(
        "m", {"type": "tasmota", "category": "meter", "host": "h", "profile": "iskra_mt175", "sensor": "MT631", "invert_sign": True}, _ctx()
    )
    f = dev._poller._fields
    assert f.sensor == "MT631" and not f.auto_sensor and f.invert_sign


def test_plug_built_from_profile() -> None:
    dev = registry.build_device(
        "dishwasher",
        {"type": "tasmota", "category": "consumer", "host": "plug.lan", "profile": "energy", "password": "pw"},
        _ctx(),
    )
    assert dev.category == DeviceCategory.CONSUMER
    assert dev._poller._poll_interval_s == 10.0
    assert dev._poller._params["password"] == "pw"


@pytest.mark.parametrize(
    "cfg",
    [
        {"profile": "energy"},
        {"host": "x"},
        {"host": "x", "fields": {"power": "A.P"}},
        {"host": "x", "profile": "shelly"},
        {"host": "x", "profile": "energy", "fields": {"power_w": None}},
    ],
)
def test_invalid_config_skips_device(cfg: dict) -> None:
    assert registry.build_device("m", {"type": "tasmota", **cfg}, _ctx()) is None

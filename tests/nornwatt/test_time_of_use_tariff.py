"""Tests for the time_of_use tariff plugin (windows, DST, seasons, composition)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from textwrap import dedent
from zoneinfo import ZoneInfo

import pytest

from nornwatt.config.yaml import YamlConfigLoader
from nornwatt.core.device_model import DeviceConfigError
from nornwatt.core.models import TariffPoint
from nornwatt.core.tariff import TariffModel
from nornwatt.loader.config_validation import validate_sections
from nornwatt.loader.device_loader import build
from nornwatt.plugins.flat_rate.tariff import FlatRateTariff
from nornwatt.plugins.time_of_use.schedule import TouSchedule, parse_schedule
from nornwatt.plugins.time_of_use.tariff import TimeOfUseTariff

BERLIN = ZoneInfo("Europe/Berlin")

DAY_NIGHT = {
    "type": "time_of_use",
    "timezone": "Europe/Berlin",
    "periods": [
        {"name": "night", "price_eur_per_kwh": 0.24, "windows": [
            {"days": ["mon-fri"], "from": "22:00", "to": "06:00"},
            {"days": ["sat", "sun"], "all_day": True},
        ]},
        {"name": "day", "price_eur_per_kwh": 0.32, "default": True},
    ],
    "seasons": [{"from": "11-01", "to": "02-28", "periods": {"day": 0.34}}],
    "export_price_eur_per_kwh": 0.08,
}


def _schedule(cfg: dict) -> TouSchedule:
    schedule, errors = parse_schedule(cfg)
    assert errors == []
    assert schedule is not None
    return schedule


def _local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=BERLIN)


def _quarters(day: str) -> list[datetime]:
    """Every quarter hour (UTC) of the local day *day*, however long it is."""
    start = _local(f"{day}T00:00").astimezone(UTC)
    end = (_local(f"{day}T00:00") + timedelta(days=1)).astimezone(UTC)
    out = []
    t = start
    while t < end:
        out.append(t)
        t += timedelta(minutes=15)
    return out


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestDayNight:
    s = _schedule(DAY_NIGHT)

    @pytest.mark.parametrize(("when", "period"), [
        ("2026-06-01T05:45", "night"),   # Monday morning: the window's own day
        ("2026-06-01T06:00", "day"),
        ("2026-06-01T21:45", "day"),
        ("2026-06-01T22:00", "night"),
        ("2026-06-05T23:00", "night"),   # Friday night
        ("2026-06-06T12:00", "night"),   # Saturday
        ("2026-06-07T23:59", "night"),   # Sunday
    ])
    def test_periods(self, when: str, period: str) -> None:
        assert self.s.period_at(_local(when)) == period

    def test_season_overrides_only_the_named_period(self) -> None:
        assert self.s.price_at(_local("2026-06-01T12:00")) == 0.32
        assert self.s.price_at(_local("2026-12-01T12:00")) == 0.34   # wraps over New Year
        assert self.s.price_at(_local("2027-02-26T12:00")) == 0.34
        assert self.s.price_at(_local("2026-12-01T23:00")) == 0.24   # night unchanged

    def test_lookup_uses_the_tariff_timezone(self) -> None:
        # 20:30 UTC on a summer Monday is 22:30 in Berlin.
        assert self.s.period_at(datetime(2026, 6, 1, 20, 30, tzinfo=UTC)) == "night"


class TestDst:
    """Windows follow the wall clock: a DST day is 23 or 25 hours long."""

    def test_weekday_boundary_moves_in_utc(self) -> None:
        s = _schedule(DAY_NIGHT)
        # Monday after the spring change (CEST, UTC+2) and after the autumn change (CET).
        assert s.period_at(datetime(2026, 3, 30, 3, 45, tzinfo=UTC)) == "night"
        assert s.period_at(datetime(2026, 3, 30, 4, 0, tzinfo=UTC)) == "day"
        assert s.period_at(datetime(2026, 10, 26, 4, 45, tzinfo=UTC)) == "night"
        assert s.period_at(datetime(2026, 10, 26, 5, 0, tzinfo=UTC)) == "day"

    def test_weekend_covers_the_whole_long_and_short_day(self) -> None:
        s = _schedule(DAY_NIGHT)
        spring, autumn = _quarters("2026-03-29"), _quarters("2026-10-25")
        assert len(spring) == 23 * 4 and len(autumn) == 25 * 4
        assert {s.period_at(t) for t in spring + autumn} == {"night"}

    def test_window_over_the_changed_hour(self) -> None:
        cfg = {"periods": [
            {"name": "x", "price_eur_per_kwh": 0.1, "windows": [{"from": "01:00", "to": "04:00"}]},
            {"name": "rest", "price_eur_per_kwh": 0.3, "default": True},
        ]}
        s = _schedule(cfg)
        hours = {day: sum(s.period_at(t) == "x" for t in _quarters(day)) / 4
                 for day in ("2026-03-28", "2026-03-29", "2026-10-25")}
        # 02:00-03:00 is skipped in spring and happens twice in autumn.
        assert hours == {"2026-03-28": 3, "2026-03-29": 2, "2026-10-25": 4}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _errors(cfg: dict) -> list[str]:
    return parse_schedule(cfg)[1]


def test_overlapping_windows_are_rejected() -> None:
    cfg = {"periods": [
        {"name": "night", "price_eur_per_kwh": 0.24, "windows": [{"from": "22:00", "to": "06:00"}]},
        {"name": "peak", "price_eur_per_kwh": 0.40,
         "windows": [{"days": ["mon"], "from": "05:00", "to": "08:00"}]},
        {"name": "day", "price_eur_per_kwh": 0.32, "default": True},
    ]}
    [msg] = _errors(cfg)
    assert "'night' and 'peak' overlap" in msg and "mon 05:00" in msg


def test_uncovered_times_are_rejected_without_a_default() -> None:
    cfg = {"periods": [
        {"name": "night", "price_eur_per_kwh": 0.24,
         "windows": [{"days": ["mon-fri"], "from": "22:00", "to": "06:00"}]},
        {"name": "day", "price_eur_per_kwh": 0.32,
         "windows": [{"days": ["mon-fri"], "from": "06:00", "to": "22:00"}]},
    ]}
    [msg] = _errors(cfg)
    assert "not covered" in msg and "sat 00:00" in msg


def test_full_coverage_without_a_default_is_fine() -> None:
    cfg = {"periods": [
        {"name": "night", "price_eur_per_kwh": 0.24, "windows": [{"from": "22:00", "to": "06:00"}]},
        {"name": "day", "price_eur_per_kwh": 0.32, "windows": [{"from": "06:00", "to": "24:00"}]},
    ]}
    # 22:00-24:00 is in both windows.
    assert "'night' and 'day' overlap" in _errors(cfg)[0]
    cfg["periods"][1]["windows"][0]["to"] = "22:00"
    assert _errors(cfg) == []


@pytest.mark.parametrize(("change", "expected"), [
    ({"from": "22:10"}, "quarter hour"),
    ({"from": "25:00"}, "not a time"),
    ({"days": ["monday"]}, "unknown day"),
    ({"to": "22:00"}, "equal"),
    ({"all_day": True}, "cannot be combined"),
])
def test_bad_windows(change: dict, expected: str) -> None:
    win = {"days": ["mon"], "from": "22:00", "to": "06:00", **change}
    cfg = {"periods": [
        {"name": "night", "price_eur_per_kwh": 0.24, "windows": [win]},
        {"name": "day", "price_eur_per_kwh": 0.32, "default": True},
    ]}
    errors = _errors(cfg)
    assert any(expected in e for e in errors), errors


def test_bad_seasons() -> None:
    cfg = {**DAY_NIGHT, "seasons": [
        {"from": "11-01", "to": "02-28", "periods": {"peak": 0.4}},
        {"from": "02-01", "to": "13-01", "periods": {"day": 0.3}},
        {"from": "12-24", "to": "12-26", "periods": {"day": 0.3}},
    ]}
    errors = _errors(cfg)
    assert any("unknown period 'peak'" in e for e in errors)
    assert any("'13-01' is not a date" in e for e in errors)
    assert any("seasons 1 and 2 overlap" in e for e in errors)


def test_config_loader_reports_schedule_and_base_problems() -> None:
    raw = {"tariffs": {
        "bad": {**DAY_NIGHT, "periods": [{"name": "day", "price_eur_per_kwh": 0.3,
                                          "windows": [{"from": "06:00", "to": "22:00"}]}]},
        "fees": {"type": "time_of_use", "base": "missing", "periods": DAY_NIGHT["periods"]},
        "a": {"type": "time_of_use", "base": "b", "periods": DAY_NIGHT["periods"]},
        "b": {"type": "time_of_use", "base": "a", "periods": DAY_NIGHT["periods"]},
    }}
    errors = validate_sections(raw)
    assert any(e.startswith("tariffs: bad: periods: these times are not covered") for e in errors)
    assert "tariffs: fees: base tariff 'missing' is not a configured tariff" in errors
    assert "tariffs: a: base tariffs form a cycle (a → b → a)" in errors


def test_issue_example_loads(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(dedent("""\
        tariffs:
          household:
            type: time_of_use
            timezone: Europe/Berlin
            periods:
              - name: night
                price_eur_per_kwh: 0.24
                windows: [{ days: [mon-fri], from: "22:00", to: "06:00" }, { days: [sat, sun], all_day: true }]
              - name: day
                price_eur_per_kwh: 0.32
                default: true
            seasons:
              - { from: "11-01", to: "02-28", periods: { day: 0.34 } }
            export_price_eur_per_kwh: 0.08
            default: true
    """))
    cfg = YamlConfigLoader(config).load()
    _, tariffs, _ = build(cfg)
    assert isinstance(tariffs["household"], TimeOfUseTariff)


def test_loader_rejects_overlaps(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(dedent("""\
        tariffs:
          household:
            type: time_of_use
            periods:
              - { name: a, price_eur_per_kwh: 0.2, windows: [{ from: "00:00", to: "12:00" }] }
              - { name: b, price_eur_per_kwh: 0.3, windows: [{ from: "11:00", to: "24:00" }] }
    """))
    with pytest.raises(DeviceConfigError) as exc:
        YamlConfigLoader(config).load()
    assert any("'a' and 'b' overlap" in e for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Schedules and composition
# ---------------------------------------------------------------------------


class _SpotTariff:
    """A base tariff with hourly prices for one fixed day (Europe/Berlin)."""

    tariff_id = "spot"

    def __init__(self, prices: list[float]) -> None:
        start = _local("2026-06-01T00:00")
        self.points = [TariffPoint(timestamp=start + timedelta(hours=i), price_eur_per_kwh=p)
                       for i, p in enumerate(prices)]

    async def price_at(self, dt: datetime) -> float:
        for p in reversed(self.points):
            if p.timestamp <= dt:
                return p.price_eur_per_kwh
        return 0.0

    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        return list(self.points)

    async def export_price_schedule(self, horizon: timedelta) -> list[TariffPoint]:
        return [TariffPoint(timestamp=p.timestamp, price_eur_per_kwh=0.0) for p in self.points]


GRID_FEES = {"periods": [  # §14a Modul 3 style: low / standard / high
    {"name": "low", "price_eur_per_kwh": -0.05, "windows": [{"from": "00:00", "to": "05:30"}]},
    {"name": "high", "price_eur_per_kwh": 0.04, "windows": [{"from": "17:00", "to": "20:00"}]},
    {"name": "standard", "price_eur_per_kwh": 0.0, "default": True},
]}


async def test_composed_price_is_base_plus_component() -> None:
    base = _SpotTariff([0.20 + i / 100 for i in range(24)])
    tariff = TimeOfUseTariff("household", _schedule(GRID_FEES), base=base)
    assert isinstance(tariff, TariffModel)

    assert await tariff.price_at(_local("2026-06-01T03:00")) == pytest.approx(0.23 - 0.05)
    assert await tariff.price_at(_local("2026-06-01T12:00")) == pytest.approx(0.32)
    assert await tariff.price_at(_local("2026-06-01T18:30")) == pytest.approx(0.38 + 0.04)

    sched = await tariff.price_schedule(timedelta(hours=24))
    by_ts = {p.timestamp: p.price_eur_per_kwh for p in sched}
    # Quarter-hour grid over the base's span: the low window ends at 05:30,
    # inside the base's 05:00 hour.
    assert len(sched) == 24 * 4
    assert by_ts[_local("2026-06-01T05:15")] == pytest.approx(0.25 - 0.05)
    assert by_ts[_local("2026-06-01T05:30")] == pytest.approx(0.25)
    assert sched[-1].timestamp == _local("2026-06-01T23:45")


async def test_component_passes_on_a_base_without_prices() -> None:
    base = _SpotTariff([0.0] * 24)
    tariff = TimeOfUseTariff("household", _schedule(GRID_FEES), base=base)
    assert await tariff.price_schedule(timedelta(hours=24)) == base.points


async def test_component_export_adds_to_base_export() -> None:
    base = FlatRateTariff("grid", import_price_eur_per_kwh=0.30, export_price_eur_per_kwh=0.08)
    tariff = TimeOfUseTariff("household", _schedule(GRID_FEES), base=base,
                             export_price_eur_per_kwh=0.01)
    assert await tariff.export_price_at(_local("2026-06-01T12:00")) == pytest.approx(0.09)
    export = await tariff.export_price_schedule(timedelta(hours=2))
    assert {round(p.price_eur_per_kwh, 6) for p in export} == {0.09}


async def test_standalone_schedule_is_a_quarter_hour_grid() -> None:
    tariff = TimeOfUseTariff("household", _schedule(DAY_NIGHT), export_price_eur_per_kwh=0.08)
    sched = await tariff.price_schedule(timedelta(hours=24))
    assert all(b.timestamp - a.timestamp == timedelta(minutes=15)
               for a, b in itertools.pairwise(sched))
    assert sched[-1].timestamp - sched[0].timestamp >= timedelta(hours=24)
    assert {p.price_eur_per_kwh for p in sched} <= {0.24, 0.32, 0.34}
    export = await tariff.export_price_schedule(timedelta(hours=24))
    assert {p.price_eur_per_kwh for p in export} == {0.08}


async def test_build_composes_in_dependency_order(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    # The component comes first in the file; its base is built before it.
    config.write_text(dedent("""\
        tariffs:
          household:
            type: time_of_use
            base: energy
            periods:
              - { name: low, price_eur_per_kwh: -0.05, windows: [{ from: "00:00", to: "06:00" }] }
              - { name: standard, price_eur_per_kwh: 0.0, default: true }
            default: true
          energy:
            type: flat_rate
            import_price_eur_per_kwh: 0.30
    """))
    cfg = YamlConfigLoader(config).load()
    _, tariffs, _ = build(cfg)
    assert list(tariffs) == ["household", "energy"]
    household = tariffs["household"]
    assert isinstance(household, TimeOfUseTariff) and household.base is tariffs["energy"]
    assert await household.price_at(_local("2026-06-01T03:00")) == pytest.approx(0.25)
    assert await household.price_at(_local("2026-06-01T12:00")) == pytest.approx(0.30)


def test_unquoted_times_are_clock_times(tmp_path) -> None:
    """YAML 1.1 reads 17:00 as 1020 (base 60); the config file means a time."""
    config = tmp_path / "config.yaml"
    config.write_text(dedent("""\
        tariffs:
          fees:
            type: time_of_use
            periods:
              - { name: high, price_eur_per_kwh: 0.04, windows: [{ from: 17:00, to: 21:00 }] }
              - { name: standard, price_eur_per_kwh: 0.0, default: true }
            export_price_eur_per_kwh: 0.5
    """))
    cfg = YamlConfigLoader(config).load()
    window = cfg.tariffs["fees"]["periods"][0]["windows"][0]
    assert window == {"from": "17:00", "to": "21:00"}
    assert cfg.tariffs["fees"]["export_price_eur_per_kwh"] == 0.5

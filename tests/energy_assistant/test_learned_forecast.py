"""Tests for the ``learned`` forecast plugin."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.core.config import BackendsConfig
from energy_assistant.core.models import ForecastPoint, ForecastQuantity
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.plugins import registry
from energy_assistant.plugins.learned.forecast import (
    ExcludeRule,
    LearnedConsumptionForecast,
    parse_exclude,
    parse_exclude_when,
)
from helpers.fake_ha_client import FakeHAClient

POWER = "sensor.heatpump_power"
TEMP = "sensor.outdoor_temperature"
METER = "sensor.household_meter"
WEATHER = "weather.home"
NOW = datetime(2026, 11, 15, 10, 30, tzinfo=timezone.utc)
N_DAYS = 120


def _temp_for(day_index: int) -> float:
    return -4.0 + 30.0 * ((day_index * 7) % N_DAYS) / N_DAYS


def _row(ts: datetime, value: float) -> dict:
    return {"start": ts, "mean": value, "min": value - 1.0, "max": value + 1.0}


def _make_ha(spike_days: tuple[int, ...] = ()) -> FakeHAClient:
    """HA with N_DAYS of hourly history following 3 + 2.5·max(0, 16 − T) kWh/day.

    Days are local days; *spike_days* get +5 kW of bogus load while the
    household meter reading is frozen.
    """
    ha = FakeHAClient()
    power, temp, meter = [], [], []
    today = NOW.astimezone().date()
    for i in range(N_DAYS):
        day = today - timedelta(days=N_DAYS - i)
        t = _temp_for(i)
        watts = (3.0 + 2.5 * max(0.0, 16.0 - t)) * 1000.0 / 24.0
        for h in range(24):
            # Naive local wall-clock time → UTC, so every local day gets its
            # own UTC offset (the fixture may span a DST change).
            ts = datetime(day.year, day.month, day.day, h).astimezone(timezone.utc)
            spike = i in spike_days
            power.append(_row(ts, watts + (5000.0 if spike else 0.0)))
            temp.append(_row(ts, t))
            meter.append(
                {"start": ts, "mean": 2.0, "min": 2.0, "max": 2.0} if spike
                else _row(ts, 400.0 + h)
            )
    ha.statistics = {POWER: power, TEMP: temp, METER: meter}
    return ha


def _provider(ha: FakeHAClient, **kwargs) -> LearnedConsumptionForecast:
    kwargs.setdefault("temperature_entity", TEMP)
    kwargs.setdefault("clock", lambda: NOW)
    return LearnedConsumptionForecast("hp_forecast", ha, POWER, **kwargs)


class _Fallback:
    quantity = ForecastQuantity.CONSUMPTION

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        return [ForecastPoint(timestamp=NOW, value=0.123)]


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------


def test_parse_exclude_naive_is_local_time():
    (r,) = parse_exclude([{"from": "2026-02-09 09:00", "to": "2026-02-27 18:00"}])
    assert r.start == datetime(2026, 2, 9, 9, 0).astimezone()
    assert r.contains(datetime(2026, 2, 9, 9, 0).astimezone())
    assert not r.contains(datetime(2026, 2, 27, 18, 0).astimezone())  # half-open


def test_parse_exclude_accepts_yaml_datetimes_and_rejects_reversed():
    (r,) = parse_exclude([{"from": datetime(2026, 1, 1), "to": datetime(2026, 1, 2)}])
    assert r.end - r.start == timedelta(days=1)
    with pytest.raises(ValueError):
        parse_exclude([{"from": "2026-02-02", "to": "2026-02-01"}])


def test_parse_exclude_when_rules():
    frozen, below = parse_exclude_when(
        [{"entity": METER, "condition": "frozen"}, {"entity": "sensor.x", "below": -200}]
    )
    assert frozen.matches({"mean": 2.0, "min": 2.0, "max": 2.0})
    assert not frozen.matches({"mean": 2.0, "min": 1.0, "max": 3.0})
    assert not frozen.matches({"mean": None, "min": None, "max": None})
    assert below.matches({"mean": -250.0}) and not below.matches({"mean": 10.0})
    assert ExcludeRule("e", above=5.0).matches({"mean": 6.0})
    with pytest.raises(ValueError):
        parse_exclude_when([{"entity": "sensor.x"}])
    with pytest.raises(ValueError):
        parse_exclude_when([{"entity": "sensor.x", "condition": "wobbly"}])


def test_frozen_rule_min_hours_needs_a_consecutive_run_at_one_value():
    (rule,) = parse_exclude_when([{"entity": TEMP, "condition": "frozen", "min_hours": 3}])
    t0 = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def flat(hour: int, value: float) -> dict:
        return {"start": t0 + timedelta(hours=hour), "mean": value, "min": value, "max": value}

    rows = [
        flat(0, 9.2), flat(1, 9.2),                 # only two hours — a calm night, kept
        _row(t0 + timedelta(hours=2), 9.5),          # moving again
        flat(3, 13.8), flat(4, 13.8), flat(5, 13.8), flat(6, 13.8),  # stuck
        flat(7, 14.0), flat(8, 14.1),                # flat hours at changing values — kept
        flat(12, 14.1), flat(13, 14.1),              # gap in the data breaks the run
    ]
    assert rule.excluded_hours(rows) == {t0 + timedelta(hours=h) for h in (3, 4, 5, 6)}
    # default min_hours=1 excludes every flat hour
    assert len(ExcludeRule(TEMP, frozen=True).excluded_hours(rows)) == 10


# ---------------------------------------------------------------------------
# fitting from statistics
# ---------------------------------------------------------------------------


async def test_fits_from_statistics_and_forecasts_hourly_kw():
    ha = _make_ha()
    provider = _provider(ha)
    points = await provider.get_forecast(timedelta(hours=24))

    model = provider.model
    assert model is not None and model.mode == "temperature"
    assert model.slope_kwh_per_k == pytest.approx(2.5, abs=0.1)
    assert provider.quantity is ForecastQuantity.CONSUMPTION

    # 24 h horizon + one lookahead point, hourly, starting at the current hour
    assert len(points) == 25
    assert points[0].timestamp == NOW.replace(minute=0)
    assert points[1].timestamp - points[0].timestamp == timedelta(hours=1)
    # No weather entity: the most recent observed temperature persists.
    expected_kw = model.daily_kwh(NOW.astimezone().date(), model.recent_temp) / 24.0
    assert points[0].value == pytest.approx(expected_kw, rel=0.05)


async def test_running_day_is_not_trained_on():
    ha = _make_ha()
    # A partial "today" with absurd values must not reach the fit.
    midnight = NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    for h in range(24):
        ha.statistics[POWER].append(_row((midnight + timedelta(hours=h)).astimezone(timezone.utc), 50_000.0))
    days = await _provider(ha).fetch_days()
    assert max(d.day for d in days) < NOW.astimezone().date()
    assert len(days) == N_DAYS


async def test_exclude_when_frozen_meter_drops_corrupt_days():
    spikes = (30, 31, 32, 33, 34, 35, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80)
    dirty = _provider(_make_ha(spikes))
    clean = _provider(_make_ha(spikes), exclude_when=[ExcludeRule(METER, frozen=True)])
    assert len(await dirty.fetch_days()) == N_DAYS
    assert len(await clean.fetch_days()) == N_DAYS - len(spikes)
    model = await clean.refit()
    assert model is not None
    assert model.n_outliers == 0
    assert model.r2 > 0.99


async def test_manual_exclude_range_drops_days():
    ha = _make_ha()
    midnight = NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    ranges = parse_exclude(
        [{"from": midnight - timedelta(days=10), "to": midnight - timedelta(days=7, hours=-3)}]
    )
    days = await _provider(ha, exclude=ranges).fetch_days()
    # three full days plus the day the range reaches three hours into
    assert len(days) == N_DAYS - 4


async def test_kw_history_unit():
    ha = _make_ha()
    for row in ha.statistics[POWER]:
        for key in ("mean", "min", "max"):
            row[key] /= 1000.0
    model = await _provider(ha, history_unit="kW").refit()
    assert model is not None
    assert model.slope_kwh_per_k == pytest.approx(2.5, abs=0.1)


# ---------------------------------------------------------------------------
# temperature for the horizon
# ---------------------------------------------------------------------------


async def test_weather_forecast_drives_prediction():
    cold, warm = _make_ha(), _make_ha()
    hour = NOW.replace(minute=0)
    start = hour - timedelta(hours=30)
    cold.weather[WEATHER] = [(start + timedelta(hours=h), -5.0) for h in range(120)]
    warm.weather[WEATHER] = [(start + timedelta(hours=h), 20.0) for h in range(120)]
    p_cold = await _provider(cold, weather_entity=WEATHER).get_forecast(timedelta(hours=24))
    p_warm = await _provider(warm, weather_entity=WEATHER).get_forecast(timedelta(hours=24))
    # tomorrow is fully covered by the forecast: 3 + 2.5·21 vs 3 kWh/day (± carry-forward)
    assert p_cold[-1].value == pytest.approx((3.0 + 2.5 * 21.0) / 24.0, rel=0.1)
    assert p_warm[-1].value == pytest.approx(3.0 / 24.0, rel=0.25)


async def test_observed_temperature_overrides_forecast_for_past_hours():
    ha = _make_ha()
    midnight = NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    # forecast says 20 °C all day, but every hour so far was measured at -5 °C
    ha.weather[WEATHER] = [(midnight + timedelta(hours=h), 20.0) for h in range(24)]
    elapsed = int((NOW.astimezone() - midnight).total_seconds() // 3600) + 1
    for h in range(elapsed):
        ha.statistics[TEMP].append(_row((midnight + timedelta(hours=h)).astimezone(timezone.utc), -5.0))
    provider = _provider(ha, weather_entity=WEATHER)
    await provider.get_forecast(timedelta(hours=1))
    temps = await provider._daily_temps(provider.model, {midnight.date()})  # noqa: SLF001
    expected = (elapsed * -5.0 + (24 - elapsed) * 20.0) / 24.0
    assert temps[midnight.date()] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# lifecycle: fallback, persistence, refit
# ---------------------------------------------------------------------------


async def test_fallback_used_when_backend_fails_then_retried_after_backoff():
    ha = _make_ha()
    ha.fail_statistics = True
    now = [NOW]
    provider = _provider(ha, fallback=_Fallback(), clock=lambda: now[0])

    points = await provider.get_forecast(timedelta(hours=24))
    assert [p.value for p in points] == [0.123]
    assert ha.statistics_calls == 1

    # Within the back-off window HA is left alone.
    await provider.get_forecast(timedelta(hours=24))
    assert ha.statistics_calls == 1

    ha.fail_statistics = False
    now[0] = NOW + timedelta(minutes=11)
    points = await provider.get_forecast(timedelta(hours=24))
    assert provider.model is not None
    assert len(points) == 25


async def test_no_model_and_no_fallback_yields_no_points():
    ha = FakeHAClient()  # no statistics at all
    assert await _provider(ha).get_forecast(timedelta(hours=24)) == []


async def test_model_is_persisted_and_reloaded_without_backend(tmp_path):
    path = tmp_path / "learned_models" / "hp.json"
    first = _provider(_make_ha(), model_path=path)
    await first.get_forecast(timedelta(hours=1))
    assert path.exists()

    offline = FakeHAClient()
    offline.fail_statistics = True
    second = _provider(offline, model_path=path)
    points = await second.get_forecast(timedelta(hours=24))
    assert second.model == first.model
    assert len(points) == 25
    assert offline.statistics_calls <= 1  # only today's temperature, no training fetch


async def test_stale_model_refits_in_background():
    ha = _make_ha()
    now = [NOW]
    provider = _provider(ha, refit_interval_h=6, clock=lambda: now[0])
    await provider.get_forecast(timedelta(hours=1))
    fitted_first = provider.model.fitted_at

    now[0] = NOW + timedelta(hours=1)
    await provider.get_forecast(timedelta(hours=1))
    assert provider._refit_task is None  # noqa: SLF001 — still fresh

    now[0] = NOW + timedelta(hours=7)
    await provider.get_forecast(timedelta(hours=1))  # serves the old model, schedules a refit
    assert provider._refit_task is not None  # noqa: SLF001
    await asyncio.wait_for(provider._refit_task, timeout=5)  # noqa: SLF001
    assert provider.model.fitted_at != fitted_first


# ---------------------------------------------------------------------------
# plugin registration
# ---------------------------------------------------------------------------


def _cfg() -> dict:
    return {
        "type": "learned",
        "history_entity": POWER,
        "temperature_entity": TEMP,
        "exclude": [{"from": "2026-02-09 09:00", "to": "2026-02-27 18:00"}],
        "exclude_when": [{"entity": METER, "condition": "frozen"}],
        "fallback": {
            "type": "static_profile",
            "profile": {"weekdays": [{"hour": 0, "consumed_kwh": 24.0}],
                        "weekends": [{"hour": 0, "consumed_kwh": 24.0}]},
        },
    }


def test_registry_builds_learned_forecast(tmp_path):
    ctx = BuildContext(backends=BackendsConfig(), ha_client=FakeHAClient(), data_dir=tmp_path)
    provider = registry.build_forecast("hp_forecast", _cfg(), ctx)
    assert isinstance(provider, LearnedConsumptionForecast)
    assert provider._model_path == tmp_path / "learned_models" / "hp_forecast.json"  # noqa: SLF001
    assert provider._fallback is not None  # noqa: SLF001


async def test_registry_returns_fallback_without_homeassistant_backend():
    ctx = BuildContext(backends=BackendsConfig())
    provider = registry.build_forecast("hp_forecast", _cfg(), ctx)
    assert not isinstance(provider, LearnedConsumptionForecast)
    points = await provider.get_forecast(timedelta(hours=2))
    assert points and all(p.value == pytest.approx(1.0) for p in points)


# ---------------------------------------------------------------------------
# diagnostics (Learning tab)
# ---------------------------------------------------------------------------


async def test_diagnostics_reports_model_days_and_forecast():
    spikes = (30, 31, 32)
    provider = _provider(_make_ha(spikes), exclude_when=[ExcludeRule(METER, frozen=True)])
    diag = await provider.diagnostics(timedelta(hours=24))

    assert diag["id"] == "hp_forecast"
    assert diag["status"] == "fitted"
    assert diag["model"]["mode"] == "temperature"
    by_status: dict[str, int] = {}
    for d in diag["days"]:
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1
    assert by_status == {"used": N_DAYS - 3, "excluded": 3}
    # Excluded days still show what was measured, next to what the model expects.
    excluded = [d for d in diag["days"] if d["status"] == "excluded"]
    assert all(d["kwh"] > d["model_kwh"] + 100 for d in excluded)
    assert [d["date"] for d in diag["days"]] == sorted(d["date"] for d in diag["days"])

    assert len(diag["forecast"]["points"]) == 25
    assert diag["forecast"]["days"][0]["kwh"] > 0


async def test_diagnostics_marks_outliers():
    ha = _make_ha()
    for row in ha.statistics[POWER][24 * 40: 24 * 42]:  # two days far off the curve
        for key in ("mean", "min", "max"):
            row[key] += 4000.0
    diag = await _provider(ha).diagnostics()
    assert sum(d["status"] == "outlier" for d in diag["days"]) == 2


async def test_diagnostics_fetches_history_for_a_model_loaded_from_disk(tmp_path):
    path = tmp_path / "hp.json"
    await _provider(_make_ha(), model_path=path).get_forecast(timedelta(hours=1))
    ha = _make_ha()
    restarted = _provider(ha, model_path=path)
    diag = await restarted.diagnostics()
    assert diag["status"] == "fitted"
    assert len(diag["days"]) == N_DAYS


async def test_diagnostics_without_model_reports_fallback_and_error():
    ha = FakeHAClient()
    ha.fail_statistics = True
    diag = await _provider(ha, fallback=_Fallback()).diagnostics()
    assert diag["status"] == "fallback"
    assert diag["model"] is None
    assert diag["last_error"] == "statistics unavailable"
    assert diag["days"] == [] and diag["forecast"]["points"] == []

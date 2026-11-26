"""Tests for the learned PV model and the learned_pv forecast plugin."""

from __future__ import annotations

import itertools
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from energy_assistant.core.models import ForecastPoint, ForecastQuantity
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.core.pv_model import PvModel, WeatherSeries, fit, solar_position
from energy_assistant.plugins import registry
from energy_assistant.plugins.learned_pv.forecast import LearnedPvForecast
from energy_assistant.plugins.learned_pv.open_meteo import parse_hourly

LAT, LON = 52.5, 6.4
TZ = ZoneInfo("Europe/Berlin")
T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _weather(days: int, *, every: int = 2, seed: int = 1, snow_days: set[int] = frozenset(),
             start: datetime = T0) -> WeatherSeries:
    """Synthetic hourly weather: a random cloudiness per day, clear-sky-like irradiance."""
    rng = random.Random(seed)
    starts: list[datetime] = []
    cloud_of_day: list[float] = []
    snow_of_day: list[bool] = []
    for d in range(0, days, every):
        c = rng.choice([0.0, 0.0, 0.1, 0.3, 0.6, 0.9])
        for h in range(24):
            starts.append(start + timedelta(days=d, hours=h))
            cloud_of_day.append(c)
            snow_of_day.append(d in snow_days)
    zen, _ = solar_position(starts, LAT, LON)
    ghi, dni, dhi = [], [], []
    for z, c in zip(zen, cloud_of_day):
        cz = math.cos(z)
        if cz <= 0.03:
            ghi.append(0.0); dni.append(0.0); dhi.append(0.0)
            continue
        n = (1 - c) * 1000.0 * math.exp(-0.12 / cz)
        f = (40.0 + 120.0 * c) * cz
        ghi.append(n * cz + f); dni.append(n); dhi.append(f)
    temp = [10.0] * len(starts)
    snow = [0.1 if s else 0.0 for s in snow_of_day]
    return WeatherSeries(starts, ghi, dni, dhi, temp, snow)


def _truth(weather: WeatherSeries, *, tilt=35.0, azimuth=-20.0, k=0.006, cap=4.0,
           shade=None, snow_factor=0.2, noise=0.03, seed=2) -> list[float]:
    """Production of a known plane, optionally shaded and snow-covered."""
    rng = random.Random(seed)
    model = PvModel(latitude=LAT, longitude=LON, tilt=tilt, azimuth=azimuth, k=k, cap_kw=cap)
    base = model.predict(WeatherSeries(weather.start, weather.ghi, weather.dni, weather.dhi))
    zen, saz = solar_position(weather.start, LAT, LON)
    out = []
    for i, p in enumerate(base):
        if shade is not None and shade(math.degrees(saz[i]), 90 - math.degrees(zen[i])):
            p *= 0.3
        if weather.snow_depth[i]:
            p *= snow_factor
        out.append(min(cap, max(0.0, p * (1 + rng.gauss(0, noise)))))
    return out


# ---------------------------------------------------------------- model


class TestFit:
    def test_recovers_the_plane_and_the_inverter_limit(self) -> None:
        w = _weather(365)
        m = fit(w, _truth(w), LAT, LON, tz=TZ)
        assert m is not None
        assert m.tilt == pytest.approx(35, abs=5)
        assert m.azimuth == pytest.approx(-20, abs=8)
        assert m.k == pytest.approx(0.006, rel=0.1)
        assert m.cap_kw == pytest.approx(4.0, rel=0.05)
        assert m.mae_kwh < 1.0

    def test_learns_shading_only_where_the_sun_is_blocked(self) -> None:
        w = _weather(365, every=1)
        # A tree to the south-east, low in the sky.
        def tree(az: float, el: float) -> bool:
            return -60 <= az < -30 and el < 20
        m = fit(w, _truth(w, shade=tree), LAT, LON, tz=TZ)
        assert m is not None
        shaded = [v for key, v in m.shading.items()
                  if -6 <= int(key.split(",")[0]) < -3 and int(key.split(",")[1]) < 4]
        free = [v for key, v in m.shading.items() if int(key.split(",")[0]) >= 0]
        assert shaded and max(shaded) < 0.6
        assert free and min(free) > 0.85

    def test_snow_hours_do_not_bias_the_plane_and_give_the_snow_factor(self) -> None:
        w = _weather(365, snow_days=set(range(0, 40, 2)))
        m = fit(w, _truth(w), LAT, LON, tz=TZ)
        assert m is not None
        assert m.k == pytest.approx(0.006, rel=0.1)
        assert m.snow_factor == pytest.approx(0.2, abs=0.1)
        assert m.n_snow_hours > 0

    def test_cold_clear_days_with_dark_panels_are_suspect(self) -> None:
        w = _weather(365, seed=3)
        pv = _truth(w)
        # Ten clear, cold days on which the panels stay dark (snow or fog the
        # forecast does not know about).
        clear_days = sorted({s.astimezone(TZ).date() for s, n in zip(w.start, w.dni) if n > 600})[:10]
        temp = list(w.temp)
        for i, s in enumerate(w.start):
            if s.astimezone(TZ).date() in clear_days:
                pv[i] = 0.0
                temp[i] = -2.0
        w = WeatherSeries(w.start, w.ghi, w.dni, w.dhi, temp, w.snow_depth)
        m = fit(w, pv, LAT, LON, tz=TZ)
        assert m is not None
        assert {d.isoformat() for d in clear_days} <= set(m.suspect_days)
        # The dark days must not teach the calibration that clear days are dark.
        assert all(v > 0.8 for key, v in m.calibration.items() if int(key.split(",")[0]) >= 9)

    def test_too_little_history_gives_none(self) -> None:
        w = _weather(10, every=1)
        assert fit(w, _truth(w), LAT, LON, tz=TZ, min_days=14) is None

    def test_configured_capacity_wins(self) -> None:
        w = _weather(120)
        m = fit(w, _truth(w), LAT, LON, tz=TZ, cap_kw=4.2)
        assert m is not None and m.cap_kw == 4.2
        assert max(m.predict(w)) <= 4.2

    def test_night_is_zero_and_roundtrip(self) -> None:
        w = _weather(120)
        m = fit(w, _truth(w), LAT, LON, tz=TZ)
        assert m is not None
        night = [p for s, p in zip(w.start, m.predict(w)) if s.hour in (0, 1, 2, 22, 23)]
        assert max(night) == 0.0
        again = PvModel.from_dict(json.loads(json.dumps(m.to_dict())))
        assert again.predict(w) == m.predict(w)


def test_parse_hourly_keys_by_hour_start_and_skips_gaps() -> None:
    body = {"hourly": {
        "time": ["2026-11-24T10:00", "2026-11-24T11:00", "2026-11-24T12:00"],
        "shortwave_radiation": [100.0, None, 300.0],
        "direct_normal_irradiance": [200.0, 1.0, 600.0],
        "diffuse_radiation": [50.0, 1.0, 100.0],
        "temperature_2m": [1.0, 2.0, 3.0],
        "snow_depth": [0.0, 0.0, 0.0],
        "cloud_cover": [80, 50, 0],
    }}
    weather, cloud = parse_hourly(body)
    assert weather.start == [datetime(2026, 11, 24, 9, tzinfo=UTC),
                             datetime(2026, 11, 24, 11, tzinfo=UTC)]
    assert weather.ghi == [100.0, 300.0]
    assert cloud == [80, 0]


# ---------------------------------------------------------------- plugin


class FakeWeather:
    """Open-Meteo stand-in: an archive and a forecast cut from one series."""

    def __init__(self, series: WeatherSeries) -> None:
        self.series = series
        self.forecast_calls = 0

    def _cut(self, lo: datetime, hi: datetime) -> WeatherSeries:
        idx = [i for i, s in enumerate(self.series.start) if lo <= s < hi]
        pick = lambda xs: [xs[i] for i in idx]
        return WeatherSeries(pick(self.series.start), pick(self.series.ghi), pick(self.series.dni),
                             pick(self.series.dhi), pick(self.series.temp), pick(self.series.snow_depth))

    async def archive(self, start, end) -> WeatherSeries:
        return self._cut(datetime.combine(start, datetime.min.time(), TZ),
                         datetime.combine(end + timedelta(days=1), datetime.min.time(), TZ))

    async def forecast(self, days=7, past_days=1):
        self.forecast_calls += 1
        w = self._cut(NOW - timedelta(days=past_days), NOW + timedelta(days=days))
        return w, [None] * len(w)


class FakeHA:
    """Home Assistant stand-in serving hourly PV statistics (W)."""

    def __init__(self, pv: dict[datetime, float], *, recent_w: float | None = None, sign: float = 1.0):
        self.pv = pv
        self.recent_w = recent_w
        self.sign = sign

    async def get_statistics(self, ids, start, end=None, period="hour", types=(), units=None):
        if period == "5minute":
            if self.recent_w is None:
                return {}
            return {ids[0]: [{"start": NOW - timedelta(minutes=60 - 5 * i), "mean": self.sign * self.recent_w}
                             for i in range(12)]}
        rows = [{"start": s, "mean": self.sign * v * 1000.0} for s, v in sorted(self.pv.items())
                if s >= start and (end is None or s < end)]
        return {ids[0]: rows}


NOW = datetime(2025, 6, 20, 11, 30, tzinfo=UTC)


def _setup(tmp_path: Path, **kw) -> tuple[LearnedPvForecast, FakeWeather, FakeHA]:
    series = _weather(200, every=1, start=(NOW - timedelta(days=190)).replace(minute=0))
    truth = _truth(series, noise=0.0)
    pv = {s: v for s, v in zip(series.start, truth) if s < NOW.replace(minute=0)}
    weather = FakeWeather(series)
    ha = FakeHA(pv, recent_w=kw.pop("recent_w", None), sign=kw.pop("sign", 1.0))
    provider = LearnedPvForecast(
        "pv", ha, weather, latitude=LAT, longitude=LON, history_entity="sensor.pv",
        model_path=tmp_path / "pv.json", tz=TZ, clock=lambda: NOW, **kw,
    )
    return provider, weather, ha


class TestLearnedPvForecast:
    async def test_refits_and_forecasts_hourly_from_the_current_hour(self, tmp_path: Path) -> None:
        provider, _, _ = _setup(tmp_path, intraday_correction=False)
        assert provider.quantity is ForecastQuantity.PV_GENERATION
        points = await provider.get_forecast(timedelta(hours=24))
        assert points[0].timestamp == NOW.replace(minute=0)
        assert all((b.timestamp - a.timestamp) == timedelta(hours=1) for a, b in itertools.pairwise(points))
        assert points[-1].timestamp <= NOW + timedelta(hours=24)
        assert max(p.value for p in points) > 1.0
        assert (tmp_path / "pv.json").exists()
        assert provider.model.tilt == pytest.approx(35, abs=5)

    async def test_negative_power_sensors_are_read_as_production(self, tmp_path: Path) -> None:
        provider, _, _ = _setup(tmp_path, sign=-1.0, intraday_correction=False)
        model = await provider.refit()
        assert model is not None and model.k == pytest.approx(0.006, rel=0.1)

    async def test_intraday_correction_fades_over_the_next_hours(self, tmp_path: Path) -> None:
        plain, _, _ = _setup(tmp_path / "a", intraday_correction=False)
        base = await plain.get_forecast(timedelta(hours=6))
        model_now = base[0].value
        # Fog: the last hour delivered 1.5 kW less than the model says.
        corrected, _, _ = _setup(tmp_path / "b", recent_w=(model_now - 1.5) * 1000.0)
        points = await corrected.get_forecast(timedelta(hours=6))
        drop = [b.value - p.value for p, b in zip(points, base)]
        assert drop[0] > 0.5
        assert drop[0] > drop[1] > drop[2] > 0
        diag = await corrected.diagnostics(timedelta(hours=6))
        assert diag["intraday"]["delta_kw"] < -0.5

    async def test_persisted_model_is_used_after_a_restart(self, tmp_path: Path) -> None:
        first, _, _ = _setup(tmp_path, intraday_correction=False)
        await first.get_forecast(timedelta(hours=6))
        second, _, ha = _setup(tmp_path, intraday_correction=False, refit_interval_h=1000)
        ha.pv = {}  # no history: only the stored model can serve
        points = await second.get_forecast(timedelta(hours=6))
        assert max(p.value for p in points) > 1.0

    async def test_fallback_until_a_model_exists(self, tmp_path: Path) -> None:
        class Fallback:
            quantity = ForecastQuantity.PV_GENERATION

            async def get_forecast(self, horizon):
                return [ForecastPoint(timestamp=NOW, value=0.42)]

        provider, _, _ = _setup(tmp_path, fallback=Fallback(), min_days=1000)
        points = await provider.get_forecast(timedelta(hours=6))
        assert [p.value for p in points] == [0.42]
        diag = await provider.diagnostics()
        assert diag["status"] == "fallback" and diag["kind"] == "pv"

    async def test_forecast_is_cached(self, tmp_path: Path) -> None:
        provider, weather, _ = _setup(tmp_path, intraday_correction=False)
        await provider.get_forecast(timedelta(hours=6))
        await provider.get_forecast(timedelta(hours=6))
        assert weather.forecast_calls == 1

    async def test_diagnostics_report_days_and_forecast(self, tmp_path: Path) -> None:
        provider, _, _ = _setup(tmp_path, intraday_correction=False, device_id="pv_production")
        diag = await provider.diagnostics(timedelta(hours=24))
        assert diag["category"] == "pv" and diag["device_id"] == "pv_production"
        assert diag["days"] and {"date", "kwh", "model_kwh", "status"} <= diag["days"][0].keys()
        assert diag["forecast"]["points"] and diag["forecast"]["days"]
        assert diag["model"]["tilt"] == pytest.approx(35, abs=5)

        summary = await provider.diagnostics(detail=False)
        assert "days" not in summary and "forecast" not in summary
        assert summary["model"] == diag["model"] and summary["status"] == "fitted"


def test_registry_builds_learned_pv_and_needs_home_assistant(tmp_path: Path) -> None:
    cfg = {"type": "learned_pv", "latitude": "52,5", "longitude": 13.4,
           "history_entity": "sensor.pv", "device": "pv_production"}
    ctx = BuildContext(backends=None, ha_client=object(), data_dir=tmp_path)
    provider = registry.build_forecast("pv", cfg, ctx)
    assert isinstance(provider, LearnedPvForecast)
    assert registry.build_forecast("pv", cfg, BuildContext(backends=None)) is None


async def test_learned_endpoint_links_the_pv_forecast_to_its_device() -> None:
    import types

    import httpx

    from energy_assistant.core.device_model import DeviceCategory
    from energy_assistant.server import Application

    class Provider:
        forecast_id = "pv_learned"

        async def diagnostics(self, horizon, *, detail=True):
            return {"id": "pv_learned", "kind": "pv", "device_id": None,
                    "history_entity": "sensor.pv_power"}

    class Registry:
        def by_category(self, category):
            assert category is DeviceCategory.PV
            return [types.SimpleNamespace(device_id="roof_east"),
                    types.SimpleNamespace(device_id="roof_south")]

    app = Application.__new__(Application)
    from energy_assistant.core.config import AppConfig

    app._cfg = AppConfig()
    object.__setattr__(app._cfg, "devices", {
        "roof_east": {"power_w": {"entity": "sensor.east_power"}},
        "roof_south": {"power_w": {"entity": "sensor.pv_power"}},
    })
    app._registry = Registry()
    app._forecast_providers = [Provider()]
    app._thermal_learners = {}
    app._efficiency_learners = {}
    app._horizon = timedelta(hours=24)
    transport = httpx.ASGITransport(app=app._build_api(), client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://ea") as c:
        (item,) = (await c.get("/api/learned")).json()
    assert item["device_id"] == "roof_south" and item["category"] == "pv"

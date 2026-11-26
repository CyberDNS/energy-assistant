"""Tests for the pure learned consumption model."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from nornwatt.core.learned_model import (
    DaySample,
    LearnedModel,
    aggregate_days,
    fit,
)

BERLIN = ZoneInfo("Europe/Berlin")
START = date(2026, 1, 5)  # a Monday


def _day(i: int, kwh: float, temp: float | None, shape: list[float] | None = None) -> DaySample:
    shape = shape or [1.0] * 24
    scale = kwh * 1000.0 / sum(shape)
    return DaySample(
        day=START + timedelta(days=i), kwh=kwh, temp=temp,
        hourly_w=tuple(s * scale for s in shape),
    )


def _heating_days(n: int = 120, floor: float = 3.0, slope: float = 2.5, base: float = 16.0):
    """Synthetic days following an exact heating hinge, temps sweeping -4…26 °C."""
    days = []
    for i in range(n):
        temp = -4.0 + 30.0 * ((i * 7) % n) / n
        days.append(_day(i, floor + slope * max(0.0, base - temp), temp))
    return days


# ---------------------------------------------------------------------------
# aggregate_days
# ---------------------------------------------------------------------------


def _hours(day: date, tz=BERLIN, value: float = 1000.0) -> dict[datetime, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return {(start + timedelta(hours=h)).astimezone(timezone.utc): value for h in range(24)}


def test_aggregate_groups_by_local_day_not_utc_day():
    """Hours are keyed in UTC but a 'day' is the local calendar day."""
    power = _hours(date(2026, 1, 10))
    # 23:00 UTC on the 9th is 00:00 Berlin on the 10th — must land on the 10th.
    assert min(power).day == 9
    days = aggregate_days(power, tz=BERLIN)
    assert [d.day for d in days] == [date(2026, 1, 10)]
    assert days[0].kwh == pytest.approx(24.0)


def test_aggregate_hourly_profile_uses_local_hour():
    start = datetime(2026, 1, 10, tzinfo=BERLIN)
    power = {(start + timedelta(hours=h)).astimezone(timezone.utc): float(h) for h in range(24)}
    (day,) = aggregate_days(power, tz=BERLIN)
    assert day.hourly_w == tuple(float(h) for h in range(24))


def test_aggregate_drops_incomplete_days():
    power = _hours(date(2026, 1, 10))
    for ts in sorted(power)[:5]:
        del power[ts]
    assert aggregate_days(power, tz=BERLIN) == []


def test_aggregate_drops_whole_day_with_an_excluded_hour():
    power = {**_hours(date(2026, 1, 10)), **_hours(date(2026, 1, 11))}
    bad = sorted(_hours(date(2026, 1, 10)))[3]
    days = aggregate_days(power, excluded={bad}, tz=BERLIN)
    assert [d.day for d in days] == [date(2026, 1, 11)]


def test_aggregate_temperature_needs_enough_hours():
    power = {**_hours(date(2026, 1, 10)), **_hours(date(2026, 1, 11))}
    temp = _hours(date(2026, 1, 10), value=4.0)
    temp.update(dict(list(_hours(date(2026, 1, 11), value=8.0).items())[:6]))
    first, second = aggregate_days(power, temp, tz=BERLIN)
    assert first.temp == pytest.approx(4.0)
    assert second.temp is None


# ---------------------------------------------------------------------------
# fit — temperature mode
# ---------------------------------------------------------------------------


def test_fit_returns_none_with_too_little_history():
    assert fit(_heating_days(10)) is None


def test_fit_recovers_heating_hinge():
    model = fit(_heating_days())
    assert model is not None and model.mode == "temperature"
    assert model.direction == "heating"
    assert model.base_temp == pytest.approx(16.0, abs=0.5)
    assert model.floor_kwh == pytest.approx(3.0, abs=0.5)
    assert model.slope_kwh_per_k == pytest.approx(2.5, abs=0.1)
    assert model.r2 > 0.99


def test_fit_recovers_cooling_hinge():
    days = []
    for i in range(120):
        temp = 5.0 + 30.0 * ((i * 7) % 120) / 120
        days.append(_day(i, 1.0 + 0.8 * max(0.0, temp - 22.0), temp))
    model = fit(days)
    assert model is not None
    assert model.direction == "cooling"
    assert model.base_temp == pytest.approx(22.0, abs=0.5)
    cold_day, hot_day = date(2026, 7, 1), date(2026, 7, 2)
    assert model.base_kwh(hot_day, 32.0) > model.base_kwh(cold_day, 15.0) + 5.0


def test_fit_drops_outlier_days():
    """A handful of wildly wrong days (meter fault) must not bend the curve."""
    days = _heating_days()
    for i in (20, 21, 22, 60):
        d = days[i]
        days[i] = DaySample(day=d.day, kwh=d.kwh + 60.0, temp=d.temp, hourly_w=d.hourly_w)
    model = fit(days)
    assert model is not None
    assert model.n_outliers == 4
    assert model.slope_kwh_per_k == pytest.approx(2.5, abs=0.1)


def test_prediction_never_negative():
    model = fit(_heating_days())
    assert model is not None
    model.observe(date(2026, 6, 1), 0.0, 25.0)  # far below the curve
    model.rho = 0.9
    assert model.daily_kwh(date(2026, 6, 2), 25.0) >= 0.0


# ---------------------------------------------------------------------------
# carry-forward
# ---------------------------------------------------------------------------


def test_carry_forward_learns_persistent_error_and_decays():
    """Residuals that persist day to day yield rho > 0 and a decaying correction."""
    days = []
    for i in range(120):
        temp = -4.0 + 30.0 * ((i * 7) % 120) / 120
        offset = 4.0 if (i // 10) % 2 == 0 else -4.0  # ten-day runs above / below
        days.append(_day(i, 20.0 + 2.5 * max(0.0, 16.0 - temp) + offset, temp))
    model = fit(days)
    assert model is not None
    assert model.rho > 0.5

    last = date.fromisoformat(model.last_day)
    base = model.base_kwh(last, 5.0)
    model.observe(last, base + 4.0, 5.0)
    one = model.daily_kwh(last + timedelta(days=1), 5.0) - base
    two = model.daily_kwh(last + timedelta(days=2), 5.0) - base
    assert one == pytest.approx(model.rho * 4.0)
    assert two == pytest.approx(model.rho**2 * 4.0)
    # No correction for the observed day itself or earlier days.
    assert model.daily_kwh(last, 5.0) == pytest.approx(base)


def test_carry_forward_is_clamped():
    model = fit(_heating_days())
    assert model is not None
    model.rho, model.resid_std = 0.5, 1.0
    last = date(2026, 6, 1)
    model.observe(last, model.base_kwh(last, 5.0) + 500.0, 5.0)  # absurd spike
    bump = model.daily_kwh(last + timedelta(days=1), 5.0) - model.base_kwh(last, 5.0)
    assert bump == pytest.approx(0.5 * 3.0)


# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------


def test_shape_differs_between_active_and_idle_days():
    night = [3.0] * 6 + [1.0] * 12 + [3.0] * 6   # heating: mostly at night
    noon = [0.5] * 11 + [8.0] + [0.5] * 12        # idle: one hot-water cycle
    days = []
    for i in range(120):
        temp = -4.0 + 30.0 * ((i * 7) % 120) / 120
        active = temp < 16.0
        days.append(_day(i, 3.0 + 2.5 * max(0.0, 16.0 - temp), temp, night if active else noon))
    model = fit(days)
    assert model is not None
    monday = date(2026, 3, 2)
    cold, warm = model.shape(monday, 0.0), model.shape(monday, 25.0)
    assert sum(cold) == pytest.approx(1.0) and sum(warm) == pytest.approx(1.0)
    assert cold[2] > cold[12]
    assert warm[11] > 0.3

    # power_kw: fractions of the daily energy; summing 24 hours gives kWh/day
    total = sum(model.power_kw(datetime(2026, 3, 2, h), 0.0) for h in range(24))
    assert total == pytest.approx(model.daily_kwh(monday, 0.0))


# ---------------------------------------------------------------------------
# level mode (no temperature)
# ---------------------------------------------------------------------------


def test_level_mode_per_day_type_from_recent_days():
    days = []
    for i in range(90):
        d = START + timedelta(days=i)
        old = i < 50  # consumption stepped up after day 50
        kwh = (8.0 if d.weekday() < 5 else 12.0) + (0.0 if old else 2.0)
        days.append(_day(i, kwh, None))
    model = fit(days)
    assert model is not None and model.mode == "level"
    assert model.level_kwh["weekday"] == pytest.approx(10.0)
    assert model.level_kwh["weekend"] == pytest.approx(14.0)
    model.rho = 0.0
    assert model.daily_kwh(date(2026, 6, 6)) == pytest.approx(14.0)  # Saturday
    assert model.daily_kwh(date(2026, 6, 8)) == pytest.approx(10.0)  # Monday


def test_too_few_temperature_days_falls_back_to_level_mode():
    days = [_day(i, 10.0, 5.0 if i < 10 else None) for i in range(60)]
    model = fit(days)
    assert model is not None and model.mode == "level"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_roundtrip_through_dict():
    model = fit(_heating_days())
    assert model is not None
    restored = LearnedModel.from_dict({**model.to_dict(), "unknown_future_field": 1})
    assert restored == model

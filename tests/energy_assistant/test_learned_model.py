"""Tests for RunningRegression, LearnedConsumptionModel, and join_samples."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from energy_assistant.core.learned_model import (
    LearnedConsumptionModel,
    RunningRegression,
    day_type,
    join_samples,
)

# A Monday and a Saturday at a fixed UTC hour. fit() buckets by *local* hour
# (see test_fit_buckets_by_local_hour_not_utc_hour), so tests must compare
# against the local-time equivalent, not the literal UTC hour, to stay
# correct regardless of the machine's timezone.
_MONDAY = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)   # workday
_SATURDAY = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)  # weekend
_LOCAL_HOUR = _MONDAY.astimezone().hour


# ---------------------------------------------------------------------------
# day_type
# ---------------------------------------------------------------------------


def test_day_type_workday_and_weekend():
    assert day_type(_MONDAY) == "workday"
    assert day_type(_SATURDAY) == "weekend"


# ---------------------------------------------------------------------------
# RunningRegression
# ---------------------------------------------------------------------------


def test_running_regression_empty_predicts_zero():
    reg = RunningRegression()
    assert reg.predict(20.0) == 0.0


def test_running_regression_single_sample_predicts_mean():
    reg = RunningRegression()
    reg.add(10.0, 500.0)
    assert reg.predict(0.0) == pytest.approx(500.0)
    assert reg.predict(30.0) == pytest.approx(500.0)


def test_running_regression_no_variance_falls_back_to_mean():
    reg = RunningRegression()
    reg.add(10.0, 100.0)
    reg.add(10.0, 300.0)
    assert reg.predict(10.0) == pytest.approx(200.0)


def test_running_regression_fits_linear_relationship():
    # power_w = 1000 - 20 * temperature (colder → more heating power)
    reg = RunningRegression()
    for temp in [-10, -5, 0, 5, 10, 15, 20]:
        reg.add(float(temp), 1000.0 - 20.0 * temp)
    assert reg.predict(0.0) == pytest.approx(1000.0, rel=1e-6)
    assert reg.predict(20.0) == pytest.approx(600.0, rel=1e-6)
    assert reg.predict(-10.0) == pytest.approx(1200.0, rel=1e-6)


def test_running_regression_low_variance_falls_back_to_mean_not_noisy_slope():
    """Regression test: all samples collected within a narrow temperature
    band (e.g. one afternoon) must not produce a trusted slope — it would be
    fit almost entirely to noise and blow up when queried far outside that
    band. Below MIN_TEMPERATURE_VARIANCE, predict() should ignore x."""
    reg = RunningRegression()
    # Temperature barely moves (27.6-28.4 range, ~0.05 variance); power is
    # noisy/unrelated to temperature in this narrow window.
    for temp, power in [(27.6, 50.0), (27.8, 900.0), (28.0, 100.0), (28.2, 20.0), (28.4, 700.0)]:
        reg.add(temp, power)
    assert reg.variance_x < 1.0
    # Querying at a very different temperature must not extrapolate wildly —
    # it should just return the mean, not a huge or negative value.
    predicted = reg.predict(0.0)
    assert predicted == pytest.approx(reg.mean_y, rel=1e-6)


def test_running_regression_predict_never_extrapolates_beyond_observed_range():
    """Even with enough variance to trust a slope, predict() must not return
    a value outside [min_y, max_y] ever observed for the bucket — protects
    against implausible (e.g. negative power) extrapolation."""
    reg = RunningRegression()
    for temp in [10.0, 15.0, 20.0, 25.0, 30.0]:
        reg.add(temp, 1000.0 - 20.0 * temp)  # y in [400, 800]
    assert reg.predict(100.0) == pytest.approx(reg.min_y, rel=1e-6)
    assert reg.predict(-100.0) == pytest.approx(reg.max_y, rel=1e-6)


# ---------------------------------------------------------------------------
# LearnedConsumptionModel — fit + predict + fallback hierarchy
# ---------------------------------------------------------------------------


def _samples_for_bucket(
    ts: datetime, n: int, temp_base: float, power_base: float, anyone_home: bool
) -> list[tuple[datetime, float, float, bool]]:
    """Build *n* samples at the given bucket with a mild temperature slope."""
    out = []
    for i in range(n):
        temp = temp_base + (i % 5)
        power = power_base - 10.0 * temp
        out.append((ts, temp, power, anyone_home))
    return out


def test_predict_uses_exact_bucket_when_well_populated():
    model = LearnedConsumptionModel(min_samples_per_bucket=5)
    samples = _samples_for_bucket(_MONDAY, 20, temp_base=10.0, power_base=1000.0, anyone_home=True)
    samples += _samples_for_bucket(_MONDAY, 20, temp_base=10.0, power_base=200.0, anyone_home=False)
    model.fit(samples)

    home_pred = model._by_full[(_LOCAL_HOUR, "workday", True)].predict(10.0)
    away_pred = model._by_full[(_LOCAL_HOUR, "workday", False)].predict(10.0)
    blended = model.predict(_LOCAL_HOUR, "workday", 10.0)
    # presence probability is 0.5 here (equal counts) → blended is the midpoint
    assert blended == pytest.approx((home_pred + away_pred) / 2, rel=1e-6)


def test_predict_falls_back_to_hour_daytype_when_presence_sparse():
    model = LearnedConsumptionModel(min_samples_per_bucket=10)
    # Only 3 "home" samples (below threshold), but 20 total at this hour/day-type.
    samples = _samples_for_bucket(_MONDAY, 3, temp_base=10.0, power_base=1000.0, anyone_home=True)
    samples += _samples_for_bucket(_MONDAY, 20, temp_base=10.0, power_base=200.0, anyone_home=False)
    model.fit(samples)

    expected = model._by_hour_daytype[(_LOCAL_HOUR, "workday")].predict(10.0)
    assert model.predict(_LOCAL_HOUR, "workday", 10.0) == pytest.approx(expected, rel=1e-6)


def test_predict_falls_back_to_hour_only_when_daytype_sparse():
    model = LearnedConsumptionModel(min_samples_per_bucket=10)
    # Fewer than min_samples for (hour=10, workday) overall, but enough at hour=10 total.
    samples = _samples_for_bucket(_MONDAY, 4, temp_base=10.0, power_base=1000.0, anyone_home=True)
    samples += _samples_for_bucket(_SATURDAY, 20, temp_base=10.0, power_base=200.0, anyone_home=False)
    model.fit(samples)

    expected = model._by_hour[_LOCAL_HOUR].predict(10.0)
    assert model.predict(_LOCAL_HOUR, "workday", 10.0) == pytest.approx(expected, rel=1e-6)


def test_predict_falls_back_to_global_mean_when_nothing_populated():
    model = LearnedConsumptionModel(min_samples_per_bucket=10)
    samples = _samples_for_bucket(_MONDAY, 3, temp_base=10.0, power_base=500.0, anyone_home=True)
    model.fit(samples)
    # hour=10 has only 3 samples total, below threshold at every level except global.
    assert model.predict(10, "workday", 10.0) == pytest.approx(model._global.mean_y, rel=1e-6)


def test_predict_on_unfitted_model_returns_zero():
    model = LearnedConsumptionModel()
    assert model.predict(5, "workday", 15.0) == 0.0


def test_fit_buckets_by_local_hour_not_utc_hour():
    """Regression test: training timestamps must be localized before bucketing,
    to match predict()'s local-hour queries (the forecast plugin always
    localizes via ts.astimezone() before calling predict)."""
    ts_utc = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    model = LearnedConsumptionModel(min_samples_per_bucket=1)
    model.fit([(ts_utc, 20.0, 500.0, True)])

    local_hour = ts_utc.astimezone().hour
    assert local_hour in model._by_hour
    # Bucketing must not have used the raw UTC hour unless it happens to
    # coincide with the local hour.
    if local_hour != ts_utc.hour:
        assert ts_utc.hour not in model._by_hour


# ---------------------------------------------------------------------------
# join_samples
# ---------------------------------------------------------------------------


def test_join_samples_matches_nearest_within_gap():
    base = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    measurements = [(base, 500.0), (base + timedelta(minutes=10), 600.0)]
    temp_signal = [(base + timedelta(minutes=1), 12.0), (base + timedelta(minutes=11), 14.0)]
    presence_signal = [(base, 1.0), (base + timedelta(minutes=10), 0.0)]

    joined = join_samples(measurements, temp_signal, presence_signal)
    assert len(joined) == 2
    ts0, temp0, power0, home0 = joined[0]
    assert temp0 == pytest.approx(12.0)
    assert power0 == pytest.approx(500.0)
    assert home0 is True
    ts1, temp1, power1, home1 = joined[1]
    assert temp1 == pytest.approx(14.0)
    assert home1 is False


def test_join_samples_drops_measurements_outside_max_gap():
    base = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    measurements = [(base, 500.0)]
    temp_signal = [(base + timedelta(hours=2), 12.0)]  # way outside default 30 min gap
    presence_signal = [(base, 1.0)]

    joined = join_samples(measurements, temp_signal, presence_signal)
    assert joined == []


def test_join_samples_empty_inputs():
    assert join_samples([], [], []) == []
    base = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    assert join_samples([(base, 1.0)], [], [(base, 1.0)]) == []

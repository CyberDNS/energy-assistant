"""Tests for StaticProfileForecast."""

from __future__ import annotations

import pytest

from energy_assistant.core.models import ForecastQuantity
from energy_assistant.plugins.static_profile.forecast import (
    StaticProfileForecast,
    _normalize_profile,
)


# ---------------------------------------------------------------------------
# _normalize_profile
# ---------------------------------------------------------------------------


def test_normalize_profile_dict_passthrough():
    raw = {"weekdays": [{"hour": 0, "consumed_kwh": 1.0}]}
    assert _normalize_profile(raw) == raw


def test_normalize_profile_list_of_dicts():
    """YAML compact-notation parses as a list of single-key dicts."""
    raw = [
        {"weekdays": [{"hour": 0, "consumed_kwh": 2.0}, {"hour": 12, "consumed_kwh": 6.0}]},
        {"weekends": [{"hour": 0, "consumed_kwh": 10.0}]},
    ]
    result = _normalize_profile(raw)
    assert set(result.keys()) == {"weekdays", "weekends"}
    assert len(result["weekdays"]) == 2
    assert len(result["weekends"]) == 1


def test_normalize_profile_empty():
    assert _normalize_profile({}) == {}
    assert _normalize_profile([]) == {}
    assert _normalize_profile(None) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Power lookup via _power_for(weekday, hour)
# ---------------------------------------------------------------------------


class TestPowerFor:
    """Tests that use weekday+hour directly, avoiding timezone issues in CI."""

    def _make(self, profile: dict) -> StaticProfileForecast:
        return StaticProfileForecast(profile=profile)

    # ── Weekdays / weekends ──────────────────────────────────────────────────

    def test_single_segment_full_day(self):
        """A single entry with consumed_kwh covers the full 24 h."""
        fc = self._make({"weekdays": [{"hour": 0, "consumed_kwh": 24.0}]})
        # 24 kWh / 24 h = 1.0 kW at any hour on a weekday
        for hour in range(24):
            assert fc._power_for(0, hour) == pytest.approx(1.0)  # Monday

    def test_two_segments(self):
        """Off-peak (00–06) and peak (06–24) segments."""
        fc = self._make({
            "weekdays": [
                {"hour": 0, "consumed_kwh": 0.5},   # 0–6 h  (6 h) → 0.0833 kW
                {"hour": 6, "consumed_kwh": 9.0},   # 6–24 h (18 h) → 0.5 kW
            ]
        })
        assert fc._power_for(0, 0) == pytest.approx(0.5 / 6)    # midnight
        assert fc._power_for(0, 5) == pytest.approx(0.5 / 6)    # still off-peak
        assert fc._power_for(0, 6) == pytest.approx(9.0 / 18)   # starts peak
        assert fc._power_for(0, 23) == pytest.approx(9.0 / 18)  # last hour

    def test_five_segment_heatpump(self):
        """Profile from the config.yaml example."""
        fc = self._make({
            "weekdays": [
                {"hour": 0,  "consumed_kwh": 0.5},   # 0–6   (6 h) → 0.0833 kW
                {"hour": 6,  "consumed_kwh": 1.5},   # 6–9   (3 h) → 0.5 kW
                {"hour": 9,  "consumed_kwh": 0.8},   # 9–17  (8 h) → 0.1 kW
                {"hour": 17, "consumed_kwh": 2.5},   # 17–22 (5 h) → 0.5 kW
                {"hour": 22, "consumed_kwh": 0.5},   # 22–24 (2 h) → 0.25 kW
            ]
        })
        assert fc._power_for(0, 0)  == pytest.approx(0.5 / 6)
        assert fc._power_for(0, 5)  == pytest.approx(0.5 / 6)
        assert fc._power_for(0, 6)  == pytest.approx(1.5 / 3)
        assert fc._power_for(0, 8)  == pytest.approx(1.5 / 3)
        assert fc._power_for(0, 9)  == pytest.approx(0.8 / 8)
        assert fc._power_for(0, 16) == pytest.approx(0.8 / 8)
        assert fc._power_for(0, 17) == pytest.approx(2.5 / 5)
        assert fc._power_for(0, 21) == pytest.approx(2.5 / 5)
        assert fc._power_for(0, 22) == pytest.approx(0.5 / 2)
        assert fc._power_for(0, 23) == pytest.approx(0.5 / 2)

    def test_weekends_separate(self):
        fc = self._make({
            "weekdays": [{"hour": 0, "consumed_kwh": 6.0}],   # 0.25 kW
            "weekends": [{"hour": 0, "consumed_kwh": 24.0}],  # 1.0 kW
        })
        assert fc._power_for(0, 12) == pytest.approx(6.0 / 24)   # Monday
        assert fc._power_for(4, 12) == pytest.approx(6.0 / 24)   # Friday
        assert fc._power_for(5, 12) == pytest.approx(24.0 / 24)  # Saturday
        assert fc._power_for(6, 12) == pytest.approx(24.0 / 24)  # Sunday

    # ── Individual day names override group keys ─────────────────────────────

    def test_specific_day_overrides_weekdays(self):
        """Monday-specific profile should override the weekdays group."""
        fc = self._make({
            "weekdays": [{"hour": 0, "consumed_kwh": 6.0}],   # 0.25 kW
            "monday":   [{"hour": 0, "consumed_kwh": 24.0}],  # 1.0 kW — override
        })
        assert fc._power_for(0, 10) == pytest.approx(24.0 / 24)  # Monday → specific
        assert fc._power_for(1, 10) == pytest.approx(6.0 / 24)   # Tuesday → group
        assert fc._power_for(4, 10) == pytest.approx(6.0 / 24)   # Friday  → group

    def test_all_individual_days(self):
        """Profiles for each individual day of the week."""
        profile = {
            day: [{"hour": 0, "consumed_kwh": float(i)}]
            for i, day in enumerate(
                ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
                start=1,
            )
        }
        fc = self._make(profile)
        for weekday in range(7):
            expected_kw = (weekday + 1) / 24
            assert fc._power_for(weekday, 0) == pytest.approx(expected_kw)

    # ── Fallback behaviour ───────────────────────────────────────────────────

    def test_missing_day_type_falls_back_to_first(self):
        """When no matching day type exists, fall back to the first available."""
        fc = self._make({"weekdays": [{"hour": 0, "consumed_kwh": 12.0}]})
        # Saturday has no "weekends" key — should use weekdays as fallback
        assert fc._power_for(5, 10) == pytest.approx(12.0 / 24)

    def test_empty_profile_returns_zero(self):
        fc = self._make({})
        assert fc._power_for(0, 12) == 0.0

    # ── List-of-dicts (YAML compact-notation) ───────────────────────────────

    def test_list_format_profile(self):
        """Profile parsed from YAML compact-notation (list of single-key dicts)."""
        raw_profile = [
            {"weekdays": [{"hour": 0, "consumed_kwh": 0.5}, {"hour": 6, "consumed_kwh": 1.5}]},
            {"weekends": [{"hour": 0, "consumed_kwh": 10.0}]},
        ]
        fc = StaticProfileForecast(profile=raw_profile)
        assert fc._power_for(0, 0) == pytest.approx(0.5 / 6)    # Monday midnight
        assert fc._power_for(0, 6) == pytest.approx(1.5 / 18)   # Monday morning
        assert fc._power_for(6, 0) == pytest.approx(10.0 / 24)  # Sunday


# ---------------------------------------------------------------------------
# quantity property
# ---------------------------------------------------------------------------


def test_quantity():
    fc = StaticProfileForecast(profile={"weekdays": [{"hour": 0, "consumed_kwh": 1.0}]})
    assert fc.quantity == ForecastQuantity.CONSUMPTION


# ---------------------------------------------------------------------------
# get_forecast (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_forecast_returns_enough_points():
    from datetime import timedelta

    fc = StaticProfileForecast(profile={"weekdays": [{"hour": 0, "consumed_kwh": 24.0}]})
    horizon = timedelta(hours=24)
    points = await fc.get_forecast(horizon)
    # At least 24 h + 1 lookahead point
    assert len(points) >= 25


@pytest.mark.asyncio
async def test_get_forecast_timestamps_are_increasing():
    from datetime import timedelta

    fc = StaticProfileForecast(profile={"weekdays": [{"hour": 0, "consumed_kwh": 1.0}]})
    points = await fc.get_forecast(timedelta(hours=12))
    for i in range(1, len(points)):
        assert points[i].timestamp > points[i - 1].timestamp


@pytest.mark.asyncio
async def test_get_forecast_values_nonnegative():
    from datetime import timedelta

    fc = StaticProfileForecast(profile={
        "weekdays": [{"hour": 0, "consumed_kwh": 0.5}, {"hour": 6, "consumed_kwh": 2.0}],
        "weekends": [{"hour": 0, "consumed_kwh": 5.0}],
    })
    points = await fc.get_forecast(timedelta(hours=48))
    for p in points:
        assert p.value >= 0.0

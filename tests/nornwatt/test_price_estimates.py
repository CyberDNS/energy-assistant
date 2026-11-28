"""Estimated prices beyond the published ones (server
``_extend_tariff_schedule_with_daily_repeat``, #158)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nornwatt.core.models import TariffPoint
from nornwatt.server import ESTIMATED_PRICE_WEIGHT, _extend_tariff_schedule_with_daily_repeat

BERLIN = ZoneInfo("Europe/Berlin")
STEP = timedelta(minutes=15)


def _day(local_date: datetime, price_at) -> list[TariffPoint]:
    """96 quarter-hour prices of one local day; *price_at(hour)*."""
    start = local_date.replace(tzinfo=BERLIN).astimezone(UTC)
    return [
        TariffPoint(timestamp=start + i * STEP, price_eur_per_kwh=price_at((i * 15) // 60))
        for i in range(96)
    ]


def friday(hour: int) -> float:   # a high morning peak
    return 0.70 if 7 <= hour < 10 else 0.45


def saturday(hour: int) -> float:  # flat-ish
    return 0.46 if 7 <= hour < 10 else 0.44


def _published_friday_evening() -> list[TariffPoint]:
    """What the price source holds on Friday evening: all of Friday (incl. the
    past hours) and all of Saturday."""
    return _day(datetime(2026, 11, 27), friday) + _day(datetime(2026, 11, 28), saturday)


def _estimate_at(ext, est_ts, local: datetime) -> float:
    ts = local.replace(tzinfo=BERLIN).astimezone(UTC)
    assert ts in est_ts
    return next(p.price_eur_per_kwh for p in ext if p.timestamp == ts)


@pytest.mark.parametrize("now_local", [
    datetime(2026, 11, 27, 22, 45),  # Friday evening, before local midnight
    datetime(2026, 11, 28, 0, 30),   # just after it
])
def test_sunday_is_estimated_from_saturday_all_evening(now_local):
    """Friday has more points than Saturday when days are counted in UTC; the
    template used to be Friday until local midnight, copying its 0.70 morning
    peak onto Sunday, then Saturday."""
    schedule = [p for p in _published_friday_evening()
                if p.timestamp >= now_local.replace(tzinfo=BERLIN).astimezone(UTC) - timedelta(days=1)]
    now = now_local.replace(tzinfo=BERLIN).astimezone(UTC)
    ext, est_ts = _extend_tariff_schedule_with_daily_repeat(
        schedule, timedelta(hours=48), now=now, tz=BERLIN,
    )
    sunday_8 = _estimate_at(ext, est_ts, datetime(2026, 11, 29, 8, 45))
    mean = (3 * 4 * 0.46 + 21 * 4 * 0.44) / 96
    assert sunday_8 == pytest.approx(mean + ESTIMATED_PRICE_WEIGHT * (0.46 - mean))
    assert sunday_8 < 0.46


def test_estimates_keep_part_of_the_swing_around_the_mean():
    schedule = _day(datetime(2026, 11, 28), friday)
    now = datetime(2026, 11, 28, 20, 0, tzinfo=BERLIN).astimezone(UTC)
    ext, est_ts = _extend_tariff_schedule_with_daily_repeat(
        schedule, timedelta(hours=48), now=now, tz=BERLIN,
    )
    mean = (3 * 4 * 0.70 + 21 * 4 * 0.45) / 96
    peak = _estimate_at(ext, est_ts, datetime(2026, 11, 29, 8, 0))
    base = _estimate_at(ext, est_ts, datetime(2026, 11, 29, 14, 0))
    assert peak == pytest.approx(mean + ESTIMATED_PRICE_WEIGHT * (0.70 - mean))
    assert base == pytest.approx(mean + ESTIMATED_PRICE_WEIGHT * (0.45 - mean))
    # Published prices are untouched.
    assert [p for p in ext if p.timestamp not in est_ts] == schedule


def test_a_partial_latest_day_is_not_the_template():
    """Only the first hours of Saturday are known: Friday stays the template."""
    schedule = _day(datetime(2026, 11, 27), friday) + _day(datetime(2026, 11, 28), saturday)[:8]
    now = datetime(2026, 11, 27, 22, 0, tzinfo=BERLIN).astimezone(UTC)
    ext, est_ts = _extend_tariff_schedule_with_daily_repeat(
        schedule, timedelta(hours=36), now=now, tz=BERLIN,
    )
    sat_8 = _estimate_at(ext, est_ts, datetime(2026, 11, 28, 8, 0))
    assert sat_8 > 0.45  # Friday's morning peak, damped

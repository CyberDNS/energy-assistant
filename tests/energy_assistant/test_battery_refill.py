"""Tests for the PV refill after the planning horizon (server helper)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from energy_assistant.core.models import ForecastPoint, StorageConstraints
from energy_assistant.server import _battery_refill_kwh

T0 = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)


def _bat(device_id: str, max_charge_kw: float) -> StorageConstraints:
    return StorageConstraints(device_id=device_id, capacity_kwh=7.5, max_charge_kw=max_charge_kw,
                              max_discharge_kw=2.0, charge_efficiency=0.9)


def _hourly(values: list[float]) -> list[ForecastPoint]:
    return [ForecastPoint(timestamp=T0 + timedelta(hours=h), value=v) for h, v in enumerate(values)]


def test_surplus_is_shared_by_charge_power_and_capped() -> None:
    # 4 sunny hours with 5 kW PV and 1 kW load: 4 kW surplus per hour.
    pv = _hourly([0.0] * 10 + [5.0] * 4 + [0.0] * 11)
    load = _hourly([1.0] * 25)
    refill = _battery_refill_kwh(pv, load, T0, [_bat("sma", 3.0), _bat("zendure", 1.2)])
    # Share by charge power: 4 × 3/4.2 = 2.86 kW (below 3 kW) and 1.14 kW.
    assert refill["sma"] == pytest.approx(4 * 4 * 3 / 4.2 * 0.9, rel=1e-3)
    assert refill["zendure"] == pytest.approx(4 * 4 * 1.2 / 4.2 * 0.9, rel=1e-3)
    # A bigger surplus is capped at each battery's charge power.
    big = _battery_refill_kwh(_hourly([0.0] * 10 + [20.0] * 4 + [0.0] * 11), load, T0,
                              [_bat("sma", 3.0), _bat("zendure", 1.2)])
    assert big["sma"] == pytest.approx(4 * 3.0 * 0.9) and big["zendure"] == pytest.approx(4 * 1.2 * 0.9)


def test_no_surplus_gives_zero_and_a_short_forecast_gives_unknown() -> None:
    load = _hourly([2.0] * 25)
    assert _battery_refill_kwh(_hourly([1.0] * 25), load, T0, [_bat("b", 3.0)]) == {"b": 0.0}
    # PV forecast ends before the window does: unknown, not zero.
    assert _battery_refill_kwh(_hourly([5.0] * 10), load, T0, [_bat("b", 3.0)]) == {}

"""Learned battery efficiency and self-consumption: the energy balance (#158)
(core/battery_efficiency, plugins/learned/efficiency)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nornwatt.core.battery_efficiency import (
    BalanceEstimate,
    find_balance_windows,
    find_energy_windows,
    fit_balance,
)
from nornwatt.core.device_model import (
    DeviceCategory,
    DeviceConfigError,
    DeviceSpec,
    parse_devices,
)
from nornwatt.core.models import StorageConstraints
from nornwatt.loader.device_loader import validate_plugins
from nornwatt.plugins.learned.efficiency import (
    EfficiencyLearner,
    build_efficiency_learners,
)

T0 = datetime(2026, 11, 10, tzinfo=UTC)
CAP = 8.0
STEP = timedelta(minutes=5)  # what the learner reads (5-minute history)


def days(
    n: int,
    *,
    eta: float = 0.9,
    self_w: float = 30.0,
    idle_w: float = 0.0,
    trickle_w: float = 0.0,
    start: datetime = T0,
    soc0: float = 30.0,
) -> list[tuple[datetime, float | None, float | None]]:
    """*n* days of a battery with per-direction efficiency *eta* and a
    constant draw *self_w* from the stored energy: each day a charge and a
    discharge of varying size with long rests between.  SoC in whole percent.

    *idle_w*: AC reading while idle (the system's standby draw from the grid,
    which does not reach the cells).  *trickle_w*: part of each discharge is
    a 6 h trickle at this power (below the standby threshold).
    """
    out = []
    stored = CAP * soc0 / 100
    t = start
    for day in range(n):
        charge_h = 1.0 + 0.4 * (day % 3)       # 3–5.4 kWh at 3 kW
        dis_h = 1.5 + 0.5 * (day % 4)          # 1.5–3 kWh at 1 kW
        phases = [(6, 0.0), (charge_h, 3000.0), (2, 0.0), (dis_h, -1000.0)]
        if trickle_w:
            phases.append((6, -trickle_w))
        phases.append((24 - sum(h for h, _ in phases), 0.0))
        for hours, power in phases:
            for _ in range(round(hours * 3600 / STEP.total_seconds())):
                out.append((t, power if power else idle_w, float(round(stored / CAP * 100))))
                dt_h = STEP.total_seconds() / 3600
                kwh = power / 1000 * dt_h
                stored += kwh * eta if kwh > 0 else kwh / eta
                stored = max(0.0, stored - self_w / 1000 * dt_h)
                t += STEP
    return out


def fit(samples, **kw):
    return fit_balance(find_balance_windows(samples, CAP, standby_w=50, **kw), CAP)


# -- the fit -------------------------------------------------------------------


@pytest.mark.parametrize(("eta", "self_w"), [(0.9, 30.0), (0.95, 25.0), (0.9, 0.0)])
def test_balance_learns_efficiency_and_self_consumption(eta, self_w):
    est = fit(days(10, eta=eta, self_w=self_w))
    assert est is not None and est.confident
    assert est.efficiency == pytest.approx(eta, abs=0.015)
    assert est.self_consumption_w == pytest.approx(self_w, abs=6.0)


def test_balance_is_not_confident_when_the_model_does_not_fit():
    # This battery runs empty (and stays there) — the windows do not follow the model.
    est = fit(days(10, eta=0.85, self_w=60.0))
    assert est is not None and not est.confident
    assert est.rms_error_kwh > 0.03 * CAP


def test_balance_needs_enough_data():
    est = fit(days(2))
    assert est is not None and not est.confident


def test_balance_roundtrip_dict():
    est = fit(days(10))
    assert BalanceEstimate.from_dict(est.to_dict()) == est


# -- battery quirks (as seen on an SMA and a Zendure) --------------------------


def test_standby_draw_is_not_charging():
    """An idle reading of +26 W is the system's standby from the grid, not energy stored."""
    est = fit(days(10, idle_w=26.0))
    assert est.efficiency == pytest.approx(0.9, abs=0.015)


def test_trickle_discharge_counts_as_energy_out():
    """Hours of 40 W discharge (below the standby threshold) deliver real energy."""
    est = fit(days(10, trickle_w=40.0, self_w=10.0))
    assert est is not None and est.confident
    assert est.efficiency == pytest.approx(0.9, abs=0.015)


def test_missing_soc_reading_does_not_split_history():
    s = days(10)
    holes = [(t, p, None if i % 50 == 7 else soc) for i, (t, p, soc) in enumerate(s)]
    assert len(find_balance_windows(holes, CAP)) == len(find_balance_windows(s, CAP))


def test_gap_splits_history():
    s = days(3)
    cut = len(s) // 2
    gapped = s[:cut] + [(t + timedelta(minutes=30), p, soc) for t, p, soc in s[cut:]]
    gap_start, gap_end = s[cut - 1][0], gapped[cut][0]
    for w in find_balance_windows(gapped, CAP):
        assert w.end <= gap_start or w.start >= gap_end


def _rest_discharge_recharge(soc0: float, *, snap_at_full: bool = False, jump: bool = False):
    """Rest, discharge 2 kWh, recharge, rest 13 h — 5-minute samples."""
    out, stored, t = [], CAP * soc0 / 100, T0
    for hours, power in ((1, 0.0), (2, -1000.0), (1, 0.0), (2 / 0.81 / 3, 3000.0), (13, 0.0)):
        for _ in range(round(hours * 12)):
            reading = round(stored / CAP * 100) + (5 if jump and t > T0 + timedelta(hours=4) else 0)
            if snap_at_full and power > 0 and reading >= 96:
                reading = 96  # the reading lags near the top until the charge has finished
            out.append((t, power, float(min(100, reading))))
            kwh = power / 1000 / 12
            stored = min(CAP, stored + (kwh * 0.9 if kwh > 0 else kwh / 0.9))
            t += STEP
    return out


def test_full_to_full_window_survives_the_snap_to_100():
    s = _rest_discharge_recharge(100.0, snap_at_full=True)
    windows = find_balance_windows(s, CAP)
    assert len(windows) == 1 and windows[0].delta_stored_kwh == pytest.approx(0.0, abs=0.1)


def test_jump_drops_a_window_away_from_full():
    assert find_balance_windows(_rest_discharge_recharge(50.0, jump=True), CAP) == []
    assert len(find_balance_windows(_rest_discharge_recharge(50.0), CAP)) == 1


# -- learner -------------------------------------------------------------------


class FakeStorage:
    def __init__(self, samples):
        self.samples = samples

    async def query_power_soc(self, device_id, start, end):
        return [s for s in self.samples if start <= s[0] <= end]


def _sc(**kw) -> StorageConstraints:
    return StorageConstraints(
        device_id="bat", capacity_kwh=CAP, max_charge_kw=3, max_discharge_kw=3,
        **{"charge_efficiency": 0.95, "discharge_efficiency": 0.95, **kw},
    )


def test_learner_applies_only_when_learned_and_confident():
    samples = days(10, eta=0.9, self_w=30.0)
    now = samples[-1][0] + timedelta(minutes=1)

    observing = EfficiencyLearner(_sc(), FakeStorage(samples), mode="configured")
    asyncio.run(observing.refit(now))
    assert observing.confident and not observing.in_use
    assert observing.apply(_sc()) == _sc()

    learned = EfficiencyLearner(_sc(), FakeStorage(samples), mode="learned")
    asyncio.run(learned.refit(now))
    sc = learned.apply(_sc())
    assert sc.charge_efficiency == sc.discharge_efficiency == pytest.approx(0.9, abs=0.015)
    assert sc.self_consumption_w == pytest.approx(30.0, abs=6.0)

    few = EfficiencyLearner(_sc(), FakeStorage(samples[: len(samples) // 5]), mode="learned")
    asyncio.run(few.refit(now))
    assert not few.in_use and few.apply(_sc()) == _sc()
    diag = asyncio.run(few.diagnostics())
    assert diag["status"] == "collecting" and diag["kind"] == "efficiency"
    assert "windows" in diag and "series" in diag
    overview = asyncio.run(few.diagnostics(detail=False))
    assert overview["balance"] == diag["balance"]
    assert "windows" not in overview and "series" not in overview


def test_learner_keeps_the_configured_asymmetry():
    samples = days(10, eta=0.9, self_w=30.0)
    sc = _sc(charge_efficiency=0.98, discharge_efficiency=0.92)
    learner = EfficiencyLearner(sc, FakeStorage(samples), mode="learned")
    asyncio.run(learner.refit(samples[-1][0] + timedelta(minutes=1)))
    planned = learner.apply(sc)
    assert planned.charge_efficiency * planned.discharge_efficiency == pytest.approx(0.81, abs=0.03)
    assert planned.charge_efficiency > planned.discharge_efficiency
    summary = learner.summary()
    assert summary["in_use"] and summary["balance"]["confident"]
    assert summary["learned_self_consumption_w"] == planned.self_consumption_w


def test_learner_keeps_windows_beyond_measurement_history(tmp_path):
    path = tmp_path / "efficiency_bat.json"
    samples = days(10)
    now = samples[-1][0] + timedelta(minutes=1)
    first = EfficiencyLearner(_sc(), FakeStorage(samples), mode="learned", model_path=path)
    asyncio.run(first.refit(now))
    n = first.balance.windows

    # A restart has the estimate before its first refit …
    second = EfficiencyLearner(_sc(), FakeStorage([]), mode="learned", model_path=path)
    assert second.in_use
    # … and keeps the windows whose measurements were purged meanwhile.
    kept = [s for s in samples if s[0] >= T0 + timedelta(days=7)]
    asyncio.run(second.refit(now + timedelta(hours=1)))
    third = EfficiencyLearner(_sc(), FakeStorage(kept), mode="learned", model_path=path)
    asyncio.run(third.refit(now + timedelta(hours=2)))
    assert third.balance.windows == n

    # Windows older than the history window drop out.
    short = EfficiencyLearner(_sc(), FakeStorage([]), mode="learned", model_path=path,
                              history_days=2)
    asyncio.run(short.refit(now + timedelta(hours=3)))
    assert short.balance is None or short.balance.windows < n


def test_build_learners_reads_mode():
    devices = parse_devices({"bat": {
        "category": "battery", "type": "zendure",
        "params": {"capacity_kwh": 5, "max_charge_kw": 1, "max_discharge_kw": 1,
                   "efficiency": "learned", "efficiency_learning": {"history_days": 21}},
    }})
    learners = build_efficiency_learners([_sc()], devices, FakeStorage([]), None)
    assert learners["bat"].mode == "learned"
    configured = {"bat": DeviceSpec(id="bat", category=DeviceCategory.BATTERY, type="zendure")}
    assert build_efficiency_learners([_sc()], configured, FakeStorage([]), None)["bat"].mode == "configured"
    assert build_efficiency_learners([_sc()], {}, None, None) == {}


def test_min_cycles_is_no_longer_a_setting():
    with pytest.raises(DeviceConfigError, match="min_cycles"):
        validate_plugins(parse_devices({"bat": {
            "category": "battery", "type": "zendure",
            "params": {"capacity_kwh": 5, "max_charge_kw": 1, "max_discharge_kw": 1,
                       "efficiency": "learned", "efficiency_learning": {"min_cycles": 3}},
        }}))


# -- energy windows: power without SoC (imported history) ---------------------------


def seasonal_hours(days: int, *, eta: float = 0.9, self_w: float = 30.0, start: datetime = T0,
                   soc_scale: float | None = None):
    """Hourly samples ``(ts, power, soc, 3600, pos, neg)`` of a battery that
    charges at 1.5 kW for a seasonal number of hours (1 in winter, 5 in
    summer) and discharges at 1 kW in the evening back to 1 kWh.  SoC is
    None (power history without state of charge) unless *soc_scale* is
    given: then a reading that moves *soc_scale* × as much as the stored
    energy (a SoC that does not track the energy)."""
    import math as _m
    out, stored, t = [], 2.0, start
    for day in range(days):
        charge_h = 1 + round(2 + 2 * _m.sin(2 * _m.pi * day / 365))
        for hour in range(24):
            if 10 <= hour < 10 + charge_h and stored < CAP - 1.5:
                p = 1500.0
            elif hour >= 18 and stored > 1.0 + 1.0 / eta:
                p = -1000.0
            else:
                p = 0.0
            stored += (p / 1000 * eta if p > 0 else p / 1000 / eta) - self_w / 1000
            stored = max(0.0, stored)
            soc = None
            if soc_scale is not None:
                soc = round(stored / CAP * 100 * soc_scale, 1)
            out.append((t, p, soc, 3600.0, max(p, 0.0), max(-p, 0.0)))
            t += timedelta(hours=1)
    return out


def test_energy_windows_learn_without_soc():
    windows = find_energy_windows(seasonal_hours(365))
    assert len(windows) == 12 and not any(w.soc_known for w in windows)
    est = fit_balance(windows, CAP)
    assert est.confident and est.basis == "energy"
    assert est.efficiency == pytest.approx(0.9, abs=0.02)
    assert est.self_consumption_w == pytest.approx(30.0, abs=10.0)


def _five_minutes(hourly):
    """Hourly samples as Nornwatt records them: 5-minute buckets, SoC moving in between."""
    out, prev_soc = [], None
    for ts, p, soc, _, pos, neg in hourly:
        for k in range(12):
            frac = (k + 1) / 12
            s5 = soc if prev_soc is None or soc is None else prev_soc + (soc - prev_soc) * frac
            out.append((ts + timedelta(minutes=5 * k), p, None if s5 is None else round(s5, 1),
                        300.0, pos, neg))
        prev_soc = soc
    return out


def test_power_history_carries_a_battery_whose_soc_does_not_fit():
    """A year of imported power without SoC, then a month of Nornwatt's own
    data whose SoC moves 1.4 × as much as the energy: the SoC windows do not
    fit, the estimate rests on the energy windows."""
    year = seasonal_hours(330)
    own = _five_minutes(seasonal_hours(30, start=year[-1][0] + timedelta(hours=1), soc_scale=1.4))
    windows = find_energy_windows(year + own) + find_balance_windows(own, CAP)
    assert any(w.soc_known for w in windows)
    est = fit_balance(windows, CAP)
    assert est.confident and est.basis == "energy", est
    assert est.efficiency == pytest.approx(0.9, abs=0.02)


def test_soc_windows_win_when_they_fit():
    year = seasonal_hours(200)
    own = days(10, start=year[-1][0] + timedelta(hours=1))
    windows = find_energy_windows(year) + find_balance_windows(own, CAP)
    est = fit_balance(windows, CAP)
    assert est.confident and est.basis == "both" and est.soc_windows and est.energy_windows


def test_energy_windows_need_coverage():
    s = seasonal_hours(60)
    holey = [x for i, x in enumerate(s) if i % 10]   # 10 % of the hours missing
    assert find_energy_windows(holey) == []

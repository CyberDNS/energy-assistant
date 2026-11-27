"""Learned battery round-trip efficiency (core/battery_efficiency, plugins/learned/efficiency)."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from itertools import pairwise

import pytest

from nornwatt.core.battery_efficiency import (
    CycleWindow,
    EfficiencyEstimate,
    estimate,
    find_cycles,
)
from nornwatt.core.device_model import DeviceCategory, DeviceSpec, parse_devices
from nornwatt.core.models import StorageConstraints
from nornwatt.plugins.learned.efficiency import (
    EfficiencyLearner,
    build_efficiency_learners,
)

T0 = datetime(2026, 11, 10, tzinfo=timezone.utc)
CAP = 8.0
STEP = timedelta(seconds=30)


def simulate(
    phases: list[tuple[float, float]],
    *,
    eta: float,
    soc0: float = 10.0,
    start: datetime = T0,
    standby_w: float = 0.0,
    jump_at: float | None = None,
) -> list[tuple[datetime, float, float]]:
    """Samples of a battery with per-direction efficiency *eta*.

    *phases*: ``(hours, power_w)``; positive charges.  SoC is reported in
    whole percent, like real batteries.  *standby_w* is a constant AC draw
    while idle that does not reach the cells.  *jump_at*: hours after start
    at which the reading jumps by +5 points (BMS re-calibration).
    """
    out = []
    stored = CAP * soc0 / 100.0
    t = start
    offset = 0.0
    for hours, power in phases:
        n = round(hours * 3600 / STEP.total_seconds())
        for _ in range(n):
            if jump_at is not None and t >= start + timedelta(hours=jump_at) and offset == 0.0:
                offset = 5.0
            reading = round(stored / CAP * 100.0 + offset)
            out.append((t, power if power else standby_w, float(reading)))
            kwh = power * STEP.total_seconds() / 3600 / 1000
            stored += kwh * eta if kwh > 0 else kwh / eta
            t += STEP
    return out


def day_cycle(eta: float = 0.9, **kw):
    # rest 1 h, charge 3 kW for 1.5 h, rest 1 h, discharge 1.5 kW back to the start, rest 1 h
    charged = 3.0 * 1.5 * eta
    dis_h = charged * eta / 1.5
    return simulate([(1, 0), (1.5, 3000), (1, 0), (dis_h, -1500), (1, 0)], eta=eta, **kw)


def test_closed_cycle_gives_round_trip():
    cycles = find_cycles(day_cycle(0.9), CAP, standby_w=50)
    assert len(cycles) == 1
    c = cycles[0]
    assert c.energy_in_kwh == pytest.approx(4.5, abs=0.01)
    # SoC readings are whole percent: ±1 point of 8 kWh over ~4.5 kWh throughput
    assert c.round_trip == pytest.approx(0.81, abs=0.02)


def test_standby_draw_is_not_a_loss():
    cycles = find_cycles(day_cycle(0.9, standby_w=35), CAP, standby_w=50)
    assert len(cycles) == 1
    assert cycles[0].round_trip == pytest.approx(0.81, abs=0.02)


def test_soc_jump_drops_the_cycle():
    assert find_cycles(day_cycle(0.9, jump_at=2.0), CAP, standby_w=50) == []


def test_gap_splits_history():
    s = day_cycle(0.9)
    cut = len(s) // 2
    s = s[:cut] + [(t + timedelta(minutes=30), p, soc) for t, p, soc in s[cut:]]
    assert find_cycles(s, CAP, standby_w=50) == []


def test_small_cycles_ignored():
    s = simulate([(1, 0), (0.2, 3000), (1, 0), (0.36, -1500), (1, 0)], eta=0.9)
    assert find_cycles(s, CAP, standby_w=50) == []


def test_several_days_non_overlapping():
    s = []
    start = T0
    for _ in range(3):
        day = day_cycle(0.9, start=start)
        s += day
        start = day[-1][0] + STEP
    cycles = find_cycles(s, CAP, standby_w=50)
    assert len(cycles) == 3
    assert all(a.end <= b.start for a, b in pairwise(cycles))
    est = estimate(cycles, CAP)
    assert est is not None and est.confident
    assert est.round_trip == pytest.approx(0.81, abs=0.015)
    assert est.efficiency == pytest.approx(0.9, abs=0.01)


def _cycle(i: int, e_in: float, e_out: float) -> CycleWindow:
    t = T0 + timedelta(days=i)
    return CycleWindow(t, t + timedelta(hours=6), 10.0, e_in, e_out)


def test_estimate_drops_outliers_and_weights_by_throughput():
    cyc = [_cycle(0, 4, 3.2), _cycle(1, 2, 1.6), _cycle(2, 4, 3.24), _cycle(3, 3, 1.5)]
    est = estimate(cyc, CAP)
    assert est.outliers == 1
    assert est.round_trip == pytest.approx((3.2 + 1.6 + 3.24) / 10, abs=1e-4)


def test_estimate_confidence_needs_cycles_and_throughput():
    assert estimate([], CAP) is None
    assert not estimate([_cycle(0, 6, 4.8), _cycle(1, 6, 4.8)], CAP).confident
    assert not estimate([_cycle(i, 3, 2.4) for i in range(3)], CAP).confident  # 9 < 12 kWh
    assert estimate([_cycle(i, 4, 3.2) for i in range(3)], CAP).confident


def test_estimate_clamped_to_physical_range():
    est = estimate([_cycle(i, 4, 4.4) for i in range(3)], CAP)
    assert est.round_trip == 1.0
    assert est.raw_round_trip == pytest.approx(1.1)


def test_estimate_roundtrip_dict():
    est = estimate([_cycle(i, 4, 3.2) for i in range(3)], CAP)
    back = EfficiencyEstimate.from_dict(est.to_dict())
    assert back == est
    assert math.isclose(back.efficiency, math.sqrt(0.8))


# -- learner ----------------------------------------------------------------


class FakeStorage:
    def __init__(self, samples):
        self.samples = samples

    async def query_power_soc(self, device_id, start, end):
        return [s for s in self.samples if start <= s[0] <= end]


def _sc(**kw) -> StorageConstraints:
    return StorageConstraints(
        device_id="bat", capacity_kwh=CAP, max_charge_kw=3, max_discharge_kw=3,
        charge_efficiency=0.95, discharge_efficiency=0.95, **kw,
    )


def _history(days: int, start: datetime = T0):
    s = []
    for _ in range(days):
        day = day_cycle(0.9, start=start)
        s += day
        start = day[0][0] + timedelta(hours=24)
    return s


def test_learner_applies_only_when_learned_and_confident(tmp_path):
    samples = _history(3)
    now = samples[-1][0] + timedelta(minutes=1)

    observing = EfficiencyLearner(_sc(), FakeStorage(samples), mode="configured")
    asyncio.run(observing.refit(now))
    assert observing.estimate.confident
    assert observing.apply(_sc()).charge_efficiency == 0.95

    learned = EfficiencyLearner(_sc(), FakeStorage(samples), mode="learned")
    asyncio.run(learned.refit(now))
    sc = learned.apply(_sc())
    assert sc.charge_efficiency == sc.discharge_efficiency == pytest.approx(0.9, abs=0.01)

    few = EfficiencyLearner(_sc(), FakeStorage(samples[: len(samples) // 3]), mode="learned")
    asyncio.run(few.refit(now))
    assert not few.in_use
    assert few.apply(_sc()).charge_efficiency == 0.95
    diag = asyncio.run(few.diagnostics())
    assert diag["status"] == "collecting" and diag["kind"] == "efficiency"
    assert "cycles" in diag and "series" in diag
    summary = asyncio.run(few.diagnostics(detail=False))
    assert summary["status"] == "collecting" and summary["estimate"] == diag["estimate"]
    assert "cycles" not in summary and "series" not in summary


def test_learner_keeps_cycles_beyond_measurement_history(tmp_path):
    path = tmp_path / "efficiency_bat.json"
    samples = _history(3)
    now = samples[-1][0] + timedelta(minutes=1)
    first = EfficiencyLearner(_sc(), FakeStorage(samples), mode="learned", model_path=path)
    asyncio.run(first.refit(now))
    assert first.estimate.cycles == 3

    # Measurements of the first two days purged; a restart reloads the cycles.
    later = now + timedelta(days=1)
    kept = [s for s in samples if s[0] >= T0 + timedelta(days=2)]
    second = EfficiencyLearner(_sc(), FakeStorage(kept), mode="learned", model_path=path)
    assert second.estimate is not None  # loaded from disk
    asyncio.run(second.refit(later))
    assert second.estimate.cycles == 3

    # Cycles older than the history window drop out.
    third = EfficiencyLearner(
        _sc(), FakeStorage([]), mode="learned", model_path=path, history_days=1.5,
    )
    asyncio.run(third.refit(later))
    assert third.estimate is None or third.estimate.cycles < 3


def test_build_learners_reads_mode():
    devices = parse_devices({"bat": {
        "category": "battery", "type": "zendure",
        "params": {"capacity_kwh": 5, "max_charge_kw": 1, "max_discharge_kw": 1,
                   "efficiency": "learned", "efficiency_learning": {"min_cycles": 5}},
    }})
    learners = build_efficiency_learners([_sc()], devices, FakeStorage([]), None)
    assert learners["bat"].mode == "learned"
    configured = {"bat": DeviceSpec(id="bat", category=DeviceCategory.BATTERY, type="zendure")}
    assert build_efficiency_learners([_sc()], configured, FakeStorage([]), None)["bat"].mode == "configured"
    assert build_efficiency_learners([_sc()], {}, None, None) == {}


# -- battery quirks (as seen on an SMA home battery) --------------------------


def _snap_to_full(samples, hold_from=96.0):
    """The reading lags near the top while charging and snaps to 100 % once
    the charge has finished: every charge to full has a SoC jump."""
    out = []
    for i, (t, p, soc) in enumerate(samples):
        charging_to_full = soc >= hold_from and any(q > 0 for _, q, _ in samples[i:i + 20])
        out.append((t, p, min(soc, hold_from) if charging_to_full and soc < 100 else soc))
    return out


def full_cycle(eta: float = 0.9, start: datetime = T0, **kw):
    """Rest at full, discharge 1.5 kW for 2 h, recharge 3 kW to full, rest."""
    out_kwh = 1.5 * 2
    charge_h = out_kwh / eta / eta / 3.0
    return simulate([(1, 0), (2, -1500), (1, 0), (charge_h, 3000), (1, 0)],
                    eta=eta, soc0=100.0, start=start, **kw)


def test_full_to_full_cycle_survives_the_snap_to_100():
    s = _snap_to_full(full_cycle(0.9))
    # the snap is a jump the measured power cannot explain ...
    assert any(b[2] - a[2] > 3 for a, b in pairwise(s))
    cycles = find_cycles(s, CAP, standby_w=50, min_soc_pct=10, max_soc_pct=100)
    # ... but full is full on both ends
    assert len(cycles) == 1
    assert cycles[0].at_limit
    assert cycles[0].round_trip == pytest.approx(0.81, abs=0.02)


def test_jump_still_drops_a_cycle_away_from_full():
    # day_cycle rests at 10 %: a jump inside keeps it out (also when min SoC is 10)
    s = day_cycle(0.9, jump_at=2.0)
    assert find_cycles(s, CAP, standby_w=50, min_soc_pct=10, max_soc_pct=100) == []


def test_trickle_discharge_counts_as_energy_out():
    # part of the discharge at 40 W (below the standby threshold) for 10 h
    charged = 3.0 * 1.5 * 0.9
    trickle_kwh = 0.4
    dis_h = (charged * 0.9 - trickle_kwh) / 1.5
    s = simulate(
        [(1, 0), (1.5, 3000), (1, 0), (10, -40), (dis_h, -1500), (1, 0)],
        eta=0.9, standby_w=26,
    )
    cycles = find_cycles(s, CAP, standby_w=50)
    assert len(cycles) == 1
    assert cycles[0].energy_out_kwh == pytest.approx(charged * 0.9, abs=0.01)
    assert cycles[0].round_trip == pytest.approx(0.81, abs=0.02)


def test_missing_soc_reading_does_not_split_history():
    # 5-minute history, as the learner reads it; one bucket without SoC
    s = day_cycle(0.9)[::10]
    assert len(find_cycles(s, CAP, standby_w=50)) == 1
    s = [(t, p, None if i == 25 else soc) for i, (t, p, soc) in enumerate(s)]
    assert len(find_cycles(s, CAP, standby_w=50)) == 1

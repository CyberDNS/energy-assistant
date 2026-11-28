"""Battery efficiency and self-consumption, learned from measurements (#158).

The battery model has two loss terms (``StorageConstraints``): the
proportional conversion loss (``charge_efficiency`` / ``discharge_efficiency``)
and a constant self-consumption (``self_consumption_w``) drawn from the stored
energy at all times.  This module learns both from what the controller
records anyway: AC power and SoC, per 5 minutes.

SoC readings are coarse (whole percent), a BMS re-calibrates them now and
then, and some batteries' SoC does not track their energy well at all — so
a balance over a short interval is useless.  The **energy balance** uses
long windows instead:

* A *rest point* is a moment the battery has been idle (no counted power,
  see below) for a while, so the SoC reading has settled.
* A *window* runs from one rest point to the next one at least 12 h later.
  In it, the measured AC energy must explain the change of the stored
  energy::

      η·E_in − E_out/η − P_self·hours = ΔE_stored

  A single reading's error is bounded and does not add up, while the losses
  grow with throughput and time, so a least-squares fit over many windows
  finds η (per direction, symmetric) and P_self.
* A window with a SoC jump inside (a change the measured power cannot
  explain: BMS re-calibration) is dropped, because one of its two readings
  was wrong — except from *full* to *full*: a BMS calibrates to 100 % at
  the end of a charge (many snap from the mid 90s to 100 % there).
* The fit is confident only when the model explains the windows (RMS within
  3 % of the capacity); a battery whose SoC reading does not track its
  energy keeps its configured values.

Charging power at or below ``standby_w`` is left out: the battery's own
standby draw (always a draw, so positive) is fixed household load for the
planner and the savings accounting.  Discharging is counted down to a small
noise floor: a battery trickling 20-50 W into the house for hours delivers
real energy.  A sample without a SoC reading keeps the previous one (the
BMS reports SoC less often than power), so it does not split the history.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "BalanceEstimate",
    "BalanceWindow",
    "find_balance_windows",
    "fit_balance",
]

_MAX_GAP = timedelta(minutes=5)
"""A longer gap between two samples ends the contiguous stretch: the energy
moved during it is unknown."""

_REST = timedelta(minutes=10)
"""Idle this long before a SoC reading counts as settled."""

_JUMP_PCT = 3.0
"""SoC change (percentage points) beyond what the measured power explains,
between two samples, that marks a BMS re-calibration."""

_DISCHARGE_NOISE_W = 10.0
"""Discharge power below this is measurement noise, not energy delivered."""

_SOC_TOL = 0.5
"""Two readings within this many percentage points count as the same SoC."""


@dataclass
class _Rest:
    ts: datetime
    soc: float
    e_in: float
    e_out: float
    jumps: int


@dataclass
class _Stretch:
    """State of one contiguous run of samples."""

    e_in: float = 0.0
    e_out: float = 0.0
    jumps: int = 0
    idle_since: datetime | None = None
    rest: _Rest | None = None
    rests: list[_Rest] = field(default_factory=list)

    def close_rest(self) -> None:
        if self.rest is not None:
            self.rests.append(self.rest)
            self.rest = None


def _stretches(
    samples: Iterable[tuple[datetime, float | None, float | None]],
    capacity_kwh: float,
    standby_w: float,
) -> list[_Stretch]:
    """Contiguous runs of samples with their energy counts and rest points."""
    stretches: list[_Stretch] = []
    cur: _Stretch | None = None
    prev: tuple[datetime, float, float] | None = None
    last_soc: float | None = None

    for ts, power, soc in samples:
        if soc is None:
            soc = last_soc
        if power is None or soc is None:
            continue
        soc = last_soc = float(soc)
        p = float(power)
        if (p > 0 and p <= standby_w) or (p < 0 and -p <= min(_DISCHARGE_NOISE_W, standby_w)):
            p = 0.0
        if prev is None or ts - prev[0] > _MAX_GAP or ts <= prev[0]:
            if cur is not None:
                cur.close_rest()
            cur = _Stretch()
            stretches.append(cur)
            cur.idle_since = ts if p == 0.0 else None
            prev = (ts, p, soc)
            continue
        assert cur is not None
        hours = (ts - prev[0]).total_seconds() / 3600.0
        kwh = prev[1] * hours / 1000.0  # power held since the previous sample
        if kwh > 0:
            cur.e_in += kwh
        else:
            cur.e_out -= kwh
        # Energy-based SoC change bounds: a jump is anything well beyond them.
        explained = abs(kwh) / capacity_kwh * 100.0
        if abs(soc - prev[2]) > explained + _JUMP_PCT:
            cur.jumps += 1

        if p == 0.0:
            if cur.idle_since is None:
                cur.idle_since = ts
            if ts - cur.idle_since >= _REST:
                # Settled.  Each reading seen while idle is an anchor (the
                # stored energy cannot change much without power); it
                # follows its reading to the last time it was shown.
                if cur.rest is not None and abs(cur.rest.soc - soc) <= _SOC_TOL:
                    cur.rest.ts = ts
                else:
                    cur.close_rest()
                    cur.rest = _Rest(ts, soc, cur.e_in, cur.e_out, cur.jumps)
        else:
            cur.idle_since = None
            cur.close_rest()
        prev = (ts, p, soc)
    if cur is not None:
        cur.close_rest()
    return stretches


_BALANCE_MIN_WINDOW = timedelta(hours=12)
"""Shortest window: the losses must be large against a reading's error."""

_ETA_GRID = [0.70 + 0.0025 * i for i in range(121)]  # 0.70 … 1.00


@dataclass(frozen=True)
class BalanceWindow:
    """One window between two settled rest points."""

    start: datetime
    end: datetime
    energy_in_kwh: float
    energy_out_kwh: float
    delta_stored_kwh: float

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "energy_in_kwh": round(self.energy_in_kwh, 4),
            "energy_out_kwh": round(self.energy_out_kwh, 4),
            "delta_stored_kwh": round(self.delta_stored_kwh, 4),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BalanceWindow:
        return cls(
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            energy_in_kwh=float(d["energy_in_kwh"]),
            energy_out_kwh=float(d["energy_out_kwh"]),
            delta_stored_kwh=float(d["delta_stored_kwh"]),
        )


@dataclass
class BalanceEstimate:
    """Efficiency per direction (symmetric) and constant self-consumption."""

    efficiency: float
    self_consumption_w: float
    windows: int
    energy_in_kwh: float
    days: float
    rms_error_kwh: float
    """Root mean square of the windows' unexplained energy."""
    confident: bool
    fitted_at: str = ""

    @property
    def round_trip(self) -> float:
        return self.efficiency ** 2

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["round_trip"] = round(self.round_trip, 4)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BalanceEstimate:
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})


def find_balance_windows(
    samples: Iterable[tuple[datetime, float | None, float | None]],
    capacity_kwh: float,
    *,
    standby_w: float = 50.0,
    max_soc_pct: float = 100.0,
    min_window: timedelta = _BALANCE_MIN_WINDOW,
) -> list[BalanceWindow]:
    """Windows from rest point to rest point, at least *min_window* long,
    chained without overlap.  A window with a SoC jump inside is dropped,
    unless both ends are at full (the BMS calibrates there)."""
    if capacity_kwh <= 0:
        return []
    full = min(max_soc_pct, 100.0) - 1.0
    out: list[BalanceWindow] = []
    for st in _stretches(samples, capacity_kwh, standby_w):
        rests = st.rests
        i = 0
        while i < len(rests):
            a = rests[i]
            j = next((k for k in range(i + 1, len(rests)) if rests[k].ts - a.ts >= min_window), None)
            if j is None:
                break
            b = rests[j]
            if b.jumps == a.jumps or (a.soc >= full and b.soc >= full):
                out.append(BalanceWindow(
                    start=a.ts, end=b.ts,
                    energy_in_kwh=b.e_in - a.e_in, energy_out_kwh=b.e_out - a.e_out,
                    delta_stored_kwh=(b.soc - a.soc) / 100.0 * capacity_kwh,
                ))
            i = j
    return out


def fit_balance(
    windows: Iterable[BalanceWindow],
    capacity_kwh: float,
    *,
    min_windows: int = 6,
    min_days: float = 4.0,
    min_throughput_capacities: float = 1.5,
    max_rms_capacities: float = 0.03,
    fitted_at: datetime | None = None,
) -> BalanceEstimate | None:
    """Least-squares fit of η (per direction, symmetric) and P_self ≥ 0.

    Confident with at least *min_windows* windows over *min_days* that
    together charged *min_throughput_capacities* × the capacity, and when
    the model explains them: the windows' unexplained energy (RMS) stays
    below *max_rms_capacities* × the capacity.  A battery whose SoC reading
    does not track its energy fails that test and keeps its configured (or
    cycle-learned) values.
    """
    ws = [w for w in windows if w.hours > 0]
    if len(ws) < 2:
        return None
    best: tuple[float, float, float] | None = None  # (sse, eta, p_kw)
    for eta in _ETA_GRID:
        r = [eta * w.energy_in_kwh - w.energy_out_kwh / eta - w.delta_stored_kwh for w in ws]
        hh = sum(w.hours ** 2 for w in ws)
        p_kw = max(0.0, sum(ri * w.hours for ri, w in zip(r, ws, strict=True)) / hh)
        sse = sum((ri - p_kw * w.hours) ** 2 for ri, w in zip(r, ws, strict=True))
        if best is None or sse < best[0]:
            best = (sse, eta, p_kw)
    assert best is not None
    sse, eta, p_kw = best
    e_in = sum(w.energy_in_kwh for w in ws)
    days = (max(w.end for w in ws) - min(w.start for w in ws)).total_seconds() / 86400.0
    confident = (
        len(ws) >= min_windows and days >= min_days
        and e_in >= min_throughput_capacities * capacity_kwh
        and _ETA_GRID[0] < eta < _ETA_GRID[-1]  # not stuck at the edge of the search
        and math.sqrt(sse / len(ws)) <= max_rms_capacities * capacity_kwh
    )
    return BalanceEstimate(
        efficiency=round(eta, 4),
        self_consumption_w=round(p_kw * 1000.0, 1),
        windows=len(ws),
        energy_in_kwh=round(e_in, 3),
        days=round(days, 2),
        rms_error_kwh=round(math.sqrt(sse / len(ws)), 4),
        confident=confident,
        fitted_at=fitted_at.isoformat() if fitted_at else "",
    )

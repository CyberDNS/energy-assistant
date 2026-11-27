"""Effective battery round-trip efficiency, learned from measurements.

The configured ``charge_efficiency`` / ``discharge_efficiency`` are often
optimistic.  This module measures what a battery really returns, from the
AC power and the SoC the controller records anyway.

SoC readings are coarse (whole percent) and a BMS re-calibrates them every
now and then, so a plain "energy in vs. SoC change" balance over a short
interval is useless.  Instead the estimator uses **closed cycles**:

* A *rest point* is a moment the battery has been idle (no counted
  power, see below) for a while, so the SoC reading has settled.
* A *cycle* runs from one rest point to a later one with the **same** SoC
  reading, and moved real energy in both directions in between.  Stored
  energy is the same at both ends, so whatever came out divided by whatever
  went in is the round-trip efficiency, with no SoC error beyond the
  reading's rounding.
* A cycle with a SoC jump inside (a change the measured power cannot
  explain: BMS re-calibration) is dropped, because one of its two
  readings was wrong.  Except a cycle from *full* to *full*: a BMS
  calibrates to 100 % at the end of a charge, so both readings are right
  whatever it corrected in between (many batteries snap from the mid 90s
  to 100 % there, so every charge to full has a jump).

Charging power at or below ``standby_w`` is left out: the battery's own
standby draw (always a draw, so positive) is fixed household load for the
planner and the savings accounting, not a storage loss.  Discharging is
counted down to a small noise floor: a battery trickling 20-50 W into the
house for hours delivers real energy, and leaving it out makes the round
trip look far worse than it is.

The estimate is ``Σ out / Σ in`` over the cycles of the last days (so a cycle
counts by its throughput), after dropping cycles far from the median.  It is
split evenly, ``η_charge = η_discharge = √round_trip``: telling the two
directions apart needs SoC readings far better than batteries report.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

__all__ = [
    "CycleWindow",
    "EfficiencyEstimate",
    "estimate",
    "find_cycles",
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


@dataclass(frozen=True)
class CycleWindow:
    """One closed cycle: rest to rest at the same SoC reading."""

    start: datetime
    end: datetime
    soc_pct: float
    energy_in_kwh: float
    """AC energy charged (above the standby deadband)."""
    energy_out_kwh: float
    """AC energy discharged (above the standby deadband)."""
    at_limit: bool = False
    """The cycle starts and ends at min or max SoC, where BMS readings are
    most trustworthy (shown only; such cycles are not weighted higher)."""

    @property
    def round_trip(self) -> float:
        return self.energy_out_kwh / self.energy_in_kwh if self.energy_in_kwh > 0 else math.nan

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "soc_pct": self.soc_pct,
            "energy_in_kwh": round(self.energy_in_kwh, 4),
            "energy_out_kwh": round(self.energy_out_kwh, 4),
            "at_limit": self.at_limit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CycleWindow:
        return cls(
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            soc_pct=float(d["soc_pct"]),
            energy_in_kwh=float(d["energy_in_kwh"]),
            energy_out_kwh=float(d["energy_out_kwh"]),
            at_limit=bool(d.get("at_limit", False)),
        )


@dataclass
class EfficiencyEstimate:
    """Learned round-trip efficiency of one battery."""

    round_trip: float
    cycles: int
    """Cycles the estimate is based on (after outlier rejection)."""
    energy_in_kwh: float
    energy_out_kwh: float
    spread: float
    """Throughput-weighted standard deviation of the per-cycle round trip."""
    outliers: int
    confident: bool
    """Enough cycles and throughput to be used for planning."""
    fitted_at: str = ""
    raw_round_trip: float | None = None
    """``Σ out / Σ in`` before clamping to a physical range."""
    window_days: float = 0.0

    @property
    def efficiency(self) -> float:
        """Per-direction efficiency (charge = discharge = √round trip)."""
        return math.sqrt(self.round_trip)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["efficiency"] = self.efficiency
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EfficiencyEstimate:
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})


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


def find_cycles(
    samples: Iterable[tuple[datetime, float | None, float | None]],
    capacity_kwh: float,
    *,
    standby_w: float = 50.0,
    min_soc_pct: float = 0.0,
    max_soc_pct: float = 100.0,
    min_throughput_kwh: float | None = None,
) -> list[CycleWindow]:
    """Closed cycles in time-ordered ``(timestamp, power_w, soc_pct)`` samples.

    ``power_w`` is AC power, positive while charging.  A sample without a
    SoC reading keeps the previous one (the BMS reports SoC less often than
    power), so it does not split the history.  *min_throughput_kwh*
    (default 15 % of the capacity) is how much energy a cycle must move
    in **each** direction: small cycles are dominated by the SoC rounding.
    """
    if capacity_kwh <= 0:
        return []
    min_kwh = min_throughput_kwh if min_throughput_kwh is not None else 0.15 * capacity_kwh

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

    def at_full(s: float) -> bool:
        return s >= min(max_soc_pct, 100.0) - 1.0

    def at_limit(s: float) -> bool:
        return s <= min_soc_pct + 1.0 or at_full(s)

    # For each rest point, the first later rest point at the same SoC with
    # enough throughput both ways; then as many of those cycles as fit
    # without overlapping (earliest end first).
    candidates: list[CycleWindow] = []
    for st in stretches:
        rests = st.rests
        for i, a in enumerate(rests):
            for b in rests[i + 1:]:
                if abs(b.soc - a.soc) > _SOC_TOL:
                    continue
                e_in, e_out = b.e_in - a.e_in, b.e_out - a.e_out
                if e_in < min_kwh or e_out < min_kwh:
                    continue
                if b.jumps == a.jumps or (at_full(a.soc) and at_full(b.soc)):
                    candidates.append(CycleWindow(
                        start=a.ts, end=b.ts, soc_pct=a.soc,
                        energy_in_kwh=e_in, energy_out_kwh=e_out,
                        at_limit=at_limit(a.soc) and at_limit(b.soc),
                    ))
                break
    # Of cycles ending together, the shortest (latest start) wins.
    candidates.sort(key=lambda w: (w.end, -w.start.timestamp()))
    chosen: list[CycleWindow] = []
    for w in candidates:
        if not chosen or w.start >= chosen[-1].end:
            chosen.append(w)
    return chosen


def estimate(
    cycles: Iterable[CycleWindow],
    capacity_kwh: float,
    *,
    min_cycles: int = 3,
    min_throughput_capacities: float = 1.5,
    fitted_at: datetime | None = None,
) -> EfficiencyEstimate | None:
    """Throughput-weighted round trip over *cycles*, or None without any.

    Cycles whose own round trip is far from the median are dropped first.
    The estimate is *confident* with at least *min_cycles* cycles that
    together charged *min_throughput_capacities* × the capacity.
    """
    cyc = [c for c in cycles if c.energy_in_kwh > 0]
    if not cyc:
        return None
    rts = [c.round_trip for c in cyc]
    med = median(rts)
    mad = median(abs(r - med) for r in rts)
    tol = max(0.08, 3.0 * 1.4826 * mad)
    kept = [c for c in cyc if abs(c.round_trip - med) <= tol]
    e_in = sum(c.energy_in_kwh for c in kept)
    e_out = sum(c.energy_out_kwh for c in kept)
    raw = e_out / e_in
    rt = min(1.0, max(0.4, raw))
    var = sum(c.energy_in_kwh * (c.round_trip - raw) ** 2 for c in kept) / e_in
    span = max(c.end for c in kept) - min(c.start for c in kept)
    return EfficiencyEstimate(
        round_trip=round(rt, 4),
        cycles=len(kept),
        energy_in_kwh=round(e_in, 3),
        energy_out_kwh=round(e_out, 3),
        spread=round(math.sqrt(var), 4),
        outliers=len(cyc) - len(kept),
        confident=len(kept) >= min_cycles and e_in >= min_throughput_capacities * capacity_kwh,
        fitted_at=fitted_at.isoformat() if fitted_at else "",
        raw_round_trip=round(raw, 4),
        window_days=round(span.total_seconds() / 86400.0, 2),
    )

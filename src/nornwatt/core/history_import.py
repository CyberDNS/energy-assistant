"""Past data from Home Assistant: hourly history before Nornwatt measured.

A device may name Home Assistant entities with its past power (and, for a
battery or a car, state of charge)::

    - id: zendure
      category: battery
      history:                       # optional: past data, hourly
        power: sensor.zendure_power
        soc: sensor.zendure_state_of_charge

Their long-term statistics (hourly, kept for good by Home Assistant) are
imported into Nornwatt's own hourly history (``history_1h``, ``source =
'imported'``), up to ``IMPORT_DAYS`` back.  Learners and charts read only that
table, so a learner starts with data instead of from zero.  A measured hour
always wins: the import never replaces one, the roll-up of measured data
replaces an imported one.

Nothing is imported unchecked — a wrong entity or an inverted sign would
teach the learners nonsense:

* **Against Nornwatt's own data** (preferred): over the hours both have,
  the entity's power must follow the measured power.  A slope near +1 is
  imported, near −1 is imported inverted (and said so), anything else or a
  weak correlation is refused with the figures.  The state of charge must
  match within a few points.
* **Battery without overlap yet**: the state of charge must rise while the
  power says charging (and fall while discharging); the opposite means the
  sign is inverted.
* Otherwise the import waits until Nornwatt has measured the device for
  ``MIN_OVERLAP_HOURS``.

Power the entity reports in both directions within one hour (it charged and
discharged) cannot be split into the two directions from hourly statistics;
such hours are left out.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

IMPORT_DAYS = 730
"""How far back the import reaches: older data hardly helps a learner."""

MIN_OVERLAP_HOURS = 48
"""Hours both sources must have before the comparison counts."""

MIN_CORRELATION = 0.9
SLOPE_RANGE = (0.8, 1.25)
"""The entity's power, scaled by the fitted slope, must explain the
measured power this well (|r|) and the |slope| must lie in this range."""

MIN_ACTIVE_HOURS = 24
"""Hours of charging or discharging the battery sign check needs."""

MAX_SOC_DIFF_PCT = 5.0
"""Mean absolute difference allowed between the two SoC series."""

_MIXED_TOLERANCE_W = 5.0
"""An hour whose minimum and maximum stay within this of zero on one side
is one-directional."""

_SETTLED_SOC_PCT = 2.0
"""An hour's mean SoC is used only when it moved less than this."""

_CHUNK = timedelta(days=90)

FIELDS = {"power": "Power", "soc": "State of charge"}
"""History fields and their labels; ``soc`` only for batteries and cars."""


@dataclass
class HourRow:
    """One imported hour (the storage's ``HistoryRow`` fields)."""

    timestamp: datetime
    pos_w: float | None
    neg_w: float | None
    soc_pct: float | None
    """The hour's SoC, only when it barely moved (a usable reading)."""
    soc_mean: float | None = None
    """The hour's mean SoC, always (for the sign check; not stored)."""


@dataclass
class Check:
    verdict: str
    """``ok``, ``inverted``, ``mismatch`` or ``waiting``."""
    message: str
    overlap_hours: int = 0
    slope: float | None = None
    correlation: float | None = None
    soc_diff_pct: float | None = None


@dataclass
class ImportStatus:
    power: str | None
    soc: str | None
    state: str
    """``imported``, ``waiting``, ``refused``, ``removed`` or ``error``."""
    message: str
    inverted: bool = False
    hours: int = 0
    first: str | None = None
    last: str | None = None
    checked_at: str = ""
    check: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- converting statistics ------------------------------------------------------


def hour_rows(
    power: list[dict[str, Any]] | None,
    soc: list[dict[str, Any]] | None,
    *,
    invert: bool = False,
) -> list[HourRow]:
    """Hourly rows from Home Assistant statistics rows (``start``, ``mean``,
    ``min``, ``max``).  *invert* flips the power's sign."""
    by_ts: dict[datetime, HourRow] = {}
    for r in power or []:
        mean, lo, hi = r.get("mean"), r.get("min"), r.get("max")
        if mean is None:
            continue
        mean, lo, hi = float(mean), float(lo if lo is not None else mean), float(hi if hi is not None else mean)
        if invert:
            mean, lo, hi = -mean, -hi, -lo
        if lo >= -_MIXED_TOLERANCE_W:
            pos, neg = max(mean, 0.0), 0.0
        elif hi <= _MIXED_TOLERANCE_W:
            pos, neg = 0.0, max(-mean, 0.0)
        else:
            pos = neg = None  # both directions in one hour: not splittable
        by_ts[r["start"]] = HourRow(r["start"], pos, neg, None)
    for r in soc or []:
        mean, lo, hi = r.get("mean"), r.get("min"), r.get("max")
        if mean is None:
            continue
        settled = lo is None or hi is None or float(hi) - float(lo) < _SETTLED_SOC_PCT
        value = float(mean) if settled else None
        row = by_ts.get(r["start"])
        if row is None:
            by_ts[r["start"]] = HourRow(r["start"], None, None, value, float(mean))
        else:
            row.soc_pct, row.soc_mean = value, float(mean)
    return [by_ts[ts] for ts in sorted(by_ts)]


# -- checks -----------------------------------------------------------------------


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope of y on x (through the origin's means) and correlation."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx <= 0 or syy <= 0:
        return math.nan, 0.0
    return sxy / sxx, sxy / math.sqrt(sxx * syy)


def _power(row: Any) -> float | None:
    if row.pos_w is None and row.neg_w is None:
        return None
    return (row.pos_w or 0.0) - (row.neg_w or 0.0)


def check_against_measured(
    raw: list[HourRow],
    measured: list[Any],
    *,
    with_soc: bool,
) -> Check:
    """Compare the entity's hours (*raw*, not inverted) with Nornwatt's own."""
    own = {m.timestamp: m for m in measured}
    pairs = [(_power(r), _power(own[r.timestamp])) for r in raw if r.timestamp in own]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(pairs) < MIN_OVERLAP_HOURS:
        return Check("waiting", f"waiting for {MIN_OVERLAP_HOURS} hours of Nornwatt's own data "
                                f"to compare with (now {len(pairs)})", overlap_hours=len(pairs))
    slope, r = _fit([x for x, _ in pairs], [y for _, y in pairs])
    soc_diff = None
    if with_soc:
        soc_pairs = [(r_.soc_pct, own[r_.timestamp].soc_pct) for r_ in raw
                     if r_.timestamp in own and r_.soc_pct is not None
                     and own[r_.timestamp].soc_pct is not None]
        if soc_pairs:
            soc_diff = sum(abs(a - b) for a, b in soc_pairs) / len(soc_pairs)
    figures: dict[str, Any] = {
        "overlap_hours": len(pairs),
        "slope": round(slope, 3) if not math.isnan(slope) else None,
        "correlation": round(r, 3),
        "soc_diff_pct": round(soc_diff, 1) if soc_diff is not None else None,
    }
    if math.isnan(slope) or abs(r) < MIN_CORRELATION:
        return Check("mismatch", f"the entity's power does not follow this device's measured power "
                                 f"(correlation {r:.2f} over {len(pairs)} hours)", **figures)
    if not SLOPE_RANGE[0] <= abs(slope) <= SLOPE_RANGE[1]:
        return Check("mismatch", f"the entity's power is {abs(slope):.2f} × this device's measured "
                                 "power — a different quantity or unit", **figures)
    if soc_diff is not None and soc_diff > MAX_SOC_DIFF_PCT:
        return Check("mismatch", f"the state of charge differs by {soc_diff:.1f} points on average",
                     **figures)
    if slope < 0:
        return Check("inverted", "matches this device's measured power with the sign inverted",
                     **figures)
    return Check("ok", "matches this device's measured power", **figures)


def check_battery_physics(raw: list[HourRow]) -> Check:
    """Without overlap: the SoC must rise while the power says charging."""
    xs, ys = [], []
    for a, b in pairwise(raw):
        p = _power(b)
        if p is None or a.soc_mean is None or b.soc_mean is None or abs(p) < 100:
            continue
        xs.append(p)
        ys.append(b.soc_mean - a.soc_mean)
    if len(xs) < MIN_ACTIVE_HOURS:
        return Check("waiting", f"waiting for {MIN_OVERLAP_HOURS} hours of Nornwatt's own data "
                                "to compare with")
    _, r = _fit(xs, ys)
    if abs(r) < 0.5:
        return Check("waiting", "the state of charge does not clearly follow the power yet; "
                                "waiting for Nornwatt's own data to compare with", correlation=round(r, 3))
    if r < 0:
        return Check("inverted", "the state of charge falls while the power says charging: "
                                 "imported with the sign inverted", correlation=round(r, 3))
    return Check("ok", "the state of charge follows the power", correlation=round(r, 3))


# -- the job ------------------------------------------------------------------------


class HistoryImporter:
    """Imports and checks one device's past data; keeps its status."""

    def __init__(self, device_id: str, category: str, source: dict[str, str],
                 ha: Any, storage: Any, data_dir: Path | None) -> None:
        self.device_id = device_id
        self.category = category
        self.power = source.get("power")
        self.soc = source.get("soc")
        self._ha = ha
        self._storage = storage
        self._path = (data_dir / "history_import" / f"{device_id}.json") if data_dir else None
        self.status = self._load()

    def _load(self) -> ImportStatus | None:
        if self._path is None or not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
            if data.get("power") == self.power and data.get("soc") == self.soc:
                return ImportStatus(**data)
        except Exception as exc:  # noqa: BLE001
            _log.warning("history import %s: could not read %s: %s", self.device_id, self._path, exc)
        return None

    def _save(self) -> None:
        if self._path is None or self.status is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self.status.to_dict()))
        except OSError as exc:
            _log.warning("history import %s: could not save %s: %s", self.device_id, self._path, exc)

    @property
    def done(self) -> bool:
        return self.status is not None and self.status.state in ("imported", "refused", "removed")

    async def _fetch(self, start: datetime, end: datetime) -> dict[str, list[dict[str, Any]]]:
        ids = [e for e in (self.power, self.soc) if e]
        out: dict[str, list[dict[str, Any]]] = {e: [] for e in ids}
        cursor = start
        while cursor < end:
            stop = min(end, cursor + _CHUNK)
            got = await self._ha.get_statistics(
                ids, start=cursor, end=stop, period="hour", types=("mean", "min", "max"),
                units={"power": "W"},
            )
            for e in ids:
                out[e].extend(got.get(e, []))
            cursor = stop
        return out

    async def run(self, now: datetime | None = None) -> ImportStatus:
        """Check the entities and import their past data, once."""
        now = now or datetime.now(UTC)
        hour = now.replace(minute=0, second=0, microsecond=0)
        status = ImportStatus(power=self.power, soc=self.soc, state="error", message="",
                              checked_at=now.isoformat())
        try:
            stats = await self._fetch(hour - timedelta(days=IMPORT_DAYS), hour)
            raw = hour_rows(stats.get(self.power or ""), stats.get(self.soc or ""))
            if not raw:
                status.message = ("Home Assistant has no long-term statistics for "
                                  + " / ".join(e for e in (self.power, self.soc) if e))
                self.status = status
                self._save()
                return status
            measured = [r for r in await self._storage.query_history(
                self.device_id, raw[0].timestamp, hour, hourly=True) if not r.imported]
            check = check_against_measured(raw, measured, with_soc=bool(self.soc))
            if check.verdict == "waiting" and self.category == "battery" and self.soc:
                physics = check_battery_physics(raw)
                if physics.verdict != "waiting":
                    check = physics
            status.check = asdict(check)
            if check.verdict == "waiting":
                status.state, status.message = "waiting", check.message[0].upper() + check.message[1:]
            elif check.verdict == "mismatch":
                status.state, status.message = "refused", "Not imported: " + check.message
            else:
                inverted = check.verdict == "inverted"
                rows = hour_rows(stats.get(self.power or ""), stats.get(self.soc or ""),
                                 invert=inverted)
                await self._storage.import_history(self.device_id, rows)
                first, last, n = await self._storage.imported_history_bounds(self.device_id)
                status.state, status.inverted, status.hours = "imported", inverted, n
                status.first = first.isoformat() if first else None
                status.last = last.isoformat() if last else None
                days = n / 24
                status.message = (f"{days:.0f} days imported — {check.message}"
                                  + (" (sign inverted)" if inverted and "inverted" not in check.message else ""))
        except Exception as exc:  # noqa: BLE001
            status.state, status.message = "error", f"Import failed: {exc}"
            _log.warning("history import %s: %s", self.device_id, exc)
        self.status = status
        self._save()
        _log.info("history import %s: %s — %s", self.device_id, status.state, status.message)
        return status

    async def remove(self) -> int:
        """Remove the imported rows.  The import does not run again by itself
        (only on request, or when the entities change)."""
        n = await self._storage.delete_imported_history(self.device_id)
        self.status = ImportStatus(power=self.power, soc=self.soc, state="removed",
                                   message=f"Imported data removed ({n} hours).",
                                   checked_at=datetime.now(UTC).isoformat())
        self._save()
        return n

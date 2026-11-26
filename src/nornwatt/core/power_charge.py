"""Power-dependent grid charges: prices that depend on the power drawn.

Some grid tariffs charge for *power*, not only for energy, measured as the
average import over a 15-minute interval at the grid connection point:

``power_threshold`` (Luxembourg "Referenzleistung", since 2025)
    Energy drawn above a subscribed power costs extra per kWh: in every
    interval, ``max(0, import_kwh − threshold_kw × 0.25 h) × excess price``.

``monthly_peak`` (Flanders "capaciteitstarief", similar demand charges)
    The highest 15-minute average import of the calendar month (local time)
    costs ``eur_per_kw_month`` per kW, billed at least on ``minimum_kw``.
    Once the month has a peak, drawing up to it costs nothing more.

They are configured on the default tariff (the one at the grid connection)
and apply to the import at the grid connection point, all tariffs together::

    tariffs:
      household:
        type: tibber
        default: true
        power_charges:
          - type: power_threshold
            threshold_kw: 7.0
            excess_eur_per_kwh: 0.11
          - type: monthly_peak
            eur_per_kw_month: 4.2
            minimum_kw: 2.5

Where they are used
-------------------
* The planner (``plugins/milp_highs``) costs the excess energy and the rise
  of the month's peak, so flexible loads move below the threshold / peak when
  that is cheaper and go above it when the alternative costs more.
* The control loop keeps the running 15-minute average below the power the
  plan accepted (``PowerChargeMeter.allowed_import_w``): like the grid
  connection limit, but the limit follows the plan's cost decision.
* The savings accounting (``core/savings``) charges both houses, so an
  avoided peak shows up as a saving.

``PowerChargeMeter`` tracks the import of the running interval and the
month's peak; the server persists it and rebuilds it from the 5-minute
history when nothing was stored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .device_model import ParamSpec

INTERVAL = timedelta(minutes=15)
"""Metering interval of the power charges."""
INTERVAL_H = INTERVAL.total_seconds() / 3600.0

POWER_THRESHOLD = "power_threshold"
MONTHLY_PEAK = "monthly_peak"

#: Component key of the power charges in savings slots.
POWER_COMPONENT = "power_charges"

_POSITIVE = {"minimum": 0, "exclusive_minimum": True}

POWER_CHARGES_SPEC = ParamSpec(
    list,
    label="Power charges",
    item_label="power charge",
    advanced=True,
    description="Charges on the power drawn (15-minute average at the grid connection), "
                "e.g. Luxembourg's power reference or Flanders' capacity tariff. Only on "
                "the default tariff.",
    items=ParamSpec(dict, summary="{type}", fields={
        "type": ParamSpec(str, required=True, label="Kind",
                          choices=(POWER_THRESHOLD, MONTHLY_PEAK),
                          choice_labels={
                              POWER_THRESHOLD: "Above a threshold",
                              MONTHLY_PEAK: "Month's peak",
                          }),
        "threshold_kw": ParamSpec(float, required=True, label="Threshold (kW)", **_POSITIVE,
                                  when={"type": POWER_THRESHOLD},
                                  description="Subscribed power; energy above it per "
                                              "15 minutes costs extra"),
        "excess_eur_per_kwh": ParamSpec(float, required=True, minimum=0,
                                        label="Extra price (€/kWh)",
                                        when={"type": POWER_THRESHOLD},
                                        description="Added to every kWh drawn above the "
                                                    "threshold"),
        "eur_per_kw_month": ParamSpec(float, required=True, minimum=0,
                                      label="Price (€/kW per month)",
                                      when={"type": MONTHLY_PEAK},
                                      description="Per kW of the highest 15-minute average "
                                                  "import of the month"),
        "minimum_kw": ParamSpec(float, minimum=0, default=0.0, label="Minimum (kW)",
                                when={"type": MONTHLY_PEAK},
                                description="Billed peak is at least this"),
    }),
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PowerThreshold:
    """Extra price per kWh drawn above ``threshold_kw`` (per interval)."""

    threshold_kw: float
    excess_eur_per_kwh: float


@dataclass(frozen=True)
class MonthlyPeak:
    """Price per kW of the month's highest interval average."""

    eur_per_kw_month: float
    minimum_kw: float = 0.0


@dataclass(frozen=True)
class PowerCharges:
    """The power charges of the grid connection."""

    thresholds: tuple[PowerThreshold, ...] = ()
    peaks: tuple[MonthlyPeak, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.thresholds or self.peaks)

    def interval_cost(self, import_kwh: float, hours: float = INTERVAL_H) -> float:
        """Threshold charges (€) of one interval with *import_kwh* drawn."""
        return sum(
            max(0.0, import_kwh - th.threshold_kw * hours) * th.excess_eur_per_kwh
            for th in self.thresholds
        )

    def peak_cost(self, old_peak_kw: float, new_peak_kw: float) -> float:
        """What raising the month's peak from *old* to *new* adds (€)."""
        return sum(
            max(0.0, max(new_peak_kw, p.minimum_kw) - max(old_peak_kw, p.minimum_kw))
            * p.eur_per_kw_month
            for p in self.peaks
        )

    def free_kw(self, month_peak_kw: float) -> float | None:
        """Average import (kW) an interval may have without any power
        charge: the lowest threshold, the month's billed peak.  None = no
        power charges."""
        limits = [th.threshold_kw for th in self.thresholds]
        limits += [max(month_peak_kw, p.minimum_kw) for p in self.peaks]
        return min(limits) if limits else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thresholds": [asdict(t) for t in self.thresholds],
            "peaks": [asdict(p) for p in self.peaks],
        }


def parse_power_charges(raw: Any) -> tuple[PowerCharges, list[str]]:
    """``power_charges:`` of a tariff → ``PowerCharges`` and problems."""
    if raw in (None, []):
        return PowerCharges(), []
    if not isinstance(raw, list):
        return PowerCharges(), ["power_charges: must be a list"]
    errors: list[str] = []
    thresholds: list[PowerThreshold] = []
    peaks: list[MonthlyPeak] = []
    for i, item in enumerate(raw):
        where = f"power_charges[{i}]"
        if not isinstance(item, Mapping):
            errors.append(f"{where}: must be a mapping")
            continue
        kind = item.get("type")
        try:
            if kind == POWER_THRESHOLD:
                th = PowerThreshold(
                    threshold_kw=float(item["threshold_kw"]),
                    excess_eur_per_kwh=float(item["excess_eur_per_kwh"]),
                )
                if th.threshold_kw <= 0 or th.excess_eur_per_kwh < 0:
                    errors.append(f"{where}: threshold_kw must be > 0, "
                                  "excess_eur_per_kwh ≥ 0")
                    continue
                thresholds.append(th)
            elif kind == MONTHLY_PEAK:
                pk = MonthlyPeak(
                    eur_per_kw_month=float(item["eur_per_kw_month"]),
                    minimum_kw=float(item.get("minimum_kw") or 0.0),
                )
                if pk.eur_per_kw_month < 0 or pk.minimum_kw < 0:
                    errors.append(f"{where}: eur_per_kw_month and minimum_kw must be ≥ 0")
                    continue
                peaks.append(pk)
            else:
                errors.append(f"{where}: type must be {POWER_THRESHOLD} or {MONTHLY_PEAK}")
        except KeyError as exc:
            errors.append(f"{where}: {exc.args[0]} is required")
        except (TypeError, ValueError):
            errors.append(f"{where}: values must be numbers")
    return PowerCharges(tuple(thresholds), tuple(peaks)), errors


def validate_tariffs(tariffs: Mapping[str, Any], default_id: str | None) -> list[str]:
    """Only the default tariff may have ``power_charges`` (their content is
    checked against ``POWER_CHARGES_SPEC`` with the other settings)."""
    return [
        f"tariffs: {tariff_id}: power_charges: only the default tariff (the one at the "
        "grid connection) can have power charges"
        for tariff_id, cfg in (tariffs or {}).items()
        if isinstance(cfg, Mapping) and cfg.get("power_charges") and tariff_id != default_id
    ]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def interval_start(ts: datetime) -> datetime:
    """Start (UTC) of the 15-minute interval *ts* falls into."""
    ts = ts.astimezone(UTC)
    return ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)


def month_key(ts: datetime) -> str:
    """Calendar month of *ts* in local time, ``YYYY-MM``."""
    local = ts.astimezone()
    return f"{local.year:04d}-{local.month:02d}"


# ---------------------------------------------------------------------------
# Meter: running interval and the month's peak
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosedInterval:
    """An interval that has ended."""

    start: datetime
    import_kwh: float
    old_peak_kw: float
    """The month's peak before this interval (0 at the start of a month)."""
    new_peak_kw: float

    @property
    def average_kw(self) -> float:
        return self.import_kwh / INTERVAL_H


@dataclass
class PowerChargeMeter:
    """Import of the running 15-minute interval and the month's peak.

    Feed it the import power between consecutive readings (``add``); it
    returns every interval that ends.  The peak is the highest interval
    average of the month (local time); a new month starts at 0.
    """

    interval: str | None = None
    """Start (UTC, ISO) of the running interval."""
    import_kwh: float = 0.0
    """Energy drawn in the running interval so far."""
    month: str | None = None
    peak_kw: float = 0.0
    peak_at: str | None = None
    """Start of the interval that set the peak (ISO)."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> PowerChargeMeter:
        data = data or {}
        return cls(
            interval=data.get("interval"),
            import_kwh=float(data.get("import_kwh") or 0.0),
            month=data.get("month"),
            peak_kw=float(data.get("peak_kw") or 0.0),
            peak_at=data.get("peak_at"),
        )

    def month_peak_kw(self, now: datetime) -> float:
        """This month's peak (0 if the stored one is of another month)."""
        return self.peak_kw if self.month == month_key(now) else 0.0

    def interval_import_kwh(self, now: datetime) -> float:
        """Energy drawn so far in the interval *now* is in."""
        return self.import_kwh if self.interval == interval_start(now).isoformat() else 0.0

    def add(self, start: datetime, end: datetime, import_kw: float) -> list[ClosedInterval]:
        """Book *import_kw* (≥ 0) over ``[start, end)``; return the
        intervals that ended, oldest first."""
        closed: list[ClosedInterval] = []
        import_kw = max(0.0, import_kw)
        t = start
        while t < end:
            iv = interval_start(t)
            iv_end = iv + INTERVAL
            if self.interval != iv.isoformat():
                if self.interval is not None:
                    closed.append(self._close())
                self.interval, self.import_kwh = iv.isoformat(), 0.0
            seg_end = min(end, iv_end)
            self.import_kwh += import_kw * (seg_end - t).total_seconds() / 3600.0
            t = seg_end
        # Ending exactly on a boundary: the interval is complete now.
        closed += self.close_open(end)
        return closed

    def close_open(self, now: datetime) -> list[ClosedInterval]:
        """Close the running interval if *now* is past it (e.g. after a gap
        without readings); nothing is booked for the gap."""
        if self.interval is None or datetime.fromisoformat(self.interval) + INTERVAL > now:
            return []
        out = [self._close()]
        self.interval, self.import_kwh = interval_start(now).isoformat(), 0.0
        return out

    def _close(self) -> ClosedInterval:
        assert self.interval is not None
        start = datetime.fromisoformat(self.interval)
        month = month_key(start)
        if self.month != month:
            self.month, self.peak_kw, self.peak_at = month, 0.0, None
        old = self.peak_kw
        avg = self.import_kwh / INTERVAL_H
        if avg > self.peak_kw:
            self.peak_kw, self.peak_at = avg, self.interval
        return ClosedInterval(start=start, import_kwh=self.import_kwh,
                              old_peak_kw=old, new_peak_kw=self.peak_kw)

    def seed_peak(self, month: str, peak_kw: float, peak_at: str | None) -> None:
        """Start from a peak found in the history (only raises it)."""
        if self.month != month:
            self.month, self.peak_kw, self.peak_at = month, 0.0, None
        if peak_kw > self.peak_kw:
            self.peak_kw, self.peak_at = peak_kw, peak_at

    def allowed_import_w(
        self, charges: PowerCharges, now: datetime, planned_kw: float | None = None,
        min_spread: timedelta = timedelta(minutes=5),
    ) -> float | None:
        """Import power (W) that keeps the running interval's average at
        the power the plan accepts.

        That power is ``free_kw`` (lowest threshold, month's billed peak), or
        the plan's import for this slot when the plan chose to go above it.
        What is left of the interval's energy budget is spread over the rest
        of the interval, but over at least *min_spread*, so the allowance
        does not jump near the interval's end.  None = no power charges.
        """
        free = charges.free_kw(self.month_peak_kw(now))
        if free is None:
            return None
        cap_kw = max(free, planned_kw or 0.0)
        iv = interval_start(now)
        remaining_h = max((iv + INTERVAL - now).total_seconds(), min_spread.total_seconds()) / 3600
        left_kwh = cap_kw * INTERVAL_H - self.interval_import_kwh(now)
        return left_kwh / remaining_h * 1000.0

    def status(self, charges: PowerCharges, now: datetime) -> dict[str, Any]:
        """For the API: the running interval and the month's peak."""
        iv = interval_start(now)
        elapsed_h = (now - iv).total_seconds() / 3600.0
        so_far = self.interval_import_kwh(now)
        return {
            "interval_start": iv.isoformat(),
            "interval_import_kwh": round(so_far, 4),
            "interval_average_kw": round(so_far / elapsed_h, 3) if elapsed_h > 1 / 360 else None,
            "month": month_key(now),
            "month_peak_kw": round(self.month_peak_kw(now), 3),
            "month_peak_at": self.peak_at if self.month == month_key(now) else None,
            "free_kw": charges.free_kw(self.month_peak_kw(now)),
            "thresholds": [asdict(t) for t in charges.thresholds],
            "peaks": [asdict(p) for p in charges.peaks],
        }


def peak_from_history(
    rows: Sequence[tuple[datetime, float | None]], now: datetime
) -> tuple[float, str | None]:
    """Highest 15-minute average import of this month from 5-minute rows
    ``(bucket start, mean import W)`` of the grid meter.  Missing buckets
    count as no import, so a gap can only lower the estimate."""
    month = month_key(now)
    current = interval_start(now)
    sums: dict[datetime, float] = {}
    for ts, import_w in rows:
        iv = interval_start(ts)
        if iv >= current or month_key(iv) != month or import_w is None:
            continue
        sums[iv] = sums.get(iv, 0.0) + max(0.0, import_w) / 1000.0 * (5 / 60)
    if not sums:
        return 0.0, None
    iv, kwh = max(sums.items(), key=lambda kv: kv[1])
    return kwh / INTERVAL_H, iv.isoformat()


# ---------------------------------------------------------------------------
# Planner input
# ---------------------------------------------------------------------------


@dataclass
class PowerChargeContext:
    """What the planner needs about the power charges (``OptimizationContext``)."""

    charges: PowerCharges
    month_peak_kw: float = 0.0
    """This month's peak so far (measured)."""
    interval_start: datetime | None = None
    """Start of the running interval."""
    interval_import_kwh: float = 0.0
    """Energy already drawn in the running interval."""

"""Time-of-use price table: parsing, validation and lookup.

A schedule maps local wall-clock time to a price:

* **periods** — named prices (``night``, ``day``, …), each with **windows**
  (weekdays plus a time range, or a whole day).  At most one period is the
  ``default``: it covers every time no window covers.  Without a default,
  every quarter hour of the week must be covered by some window.
* **seasons** — optional date ranges (``MM-DD``, inclusive, may wrap over New
  Year) that replace the price of some periods.

A window's time range applies to each listed day on its own: ``mon-fri,
22:00-06:00`` means 00:00-06:00 and 22:00-24:00 on Monday to Friday, so
Monday morning is covered too and Saturday morning is not.  Times are on
quarter hours (``HH:MM``, ``to`` may be ``24:00``).

Everything is evaluated in the configured timezone, so a window keeps its
wall-clock times across a DST change (the day is 23 or 25 hours long, the
prices follow the clock).

``parse_schedule(cfg)`` returns the schedule and a list of problems; the
config loader shows the problems, the tariff factory refuses to build when
there are any.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
QUARTER = timedelta(minutes=15)
_SLOTS_PER_DAY = 96


@dataclass(frozen=True)
class Season:
    start: tuple[int, int]   # (month, day), inclusive
    end: tuple[int, int]     # (month, day), inclusive
    prices: Mapping[int, float]  # period index → price

    def contains(self, day: date) -> bool:
        md = (day.month, day.day)
        if self.start <= self.end:
            return self.start <= md <= self.end
        return md >= self.start or md <= self.end


@dataclass(frozen=True)
class TouSchedule:
    """A parsed, valid time-of-use table."""

    tz: ZoneInfo
    names: tuple[str, ...]
    prices: tuple[float, ...]
    table: tuple[tuple[int, ...], ...]
    """``table[weekday][quarter]`` → period index (weekday 0 = Monday)."""
    seasons: tuple[Season, ...] = ()

    def period_at(self, dt: datetime) -> str:
        """Name of the period in force at *dt* (an aware datetime)."""
        return self.names[self._index(dt.astimezone(self.tz))]

    def price_at(self, dt: datetime) -> float:
        """Price (EUR/kWh) at *dt* (an aware datetime)."""
        local = dt.astimezone(self.tz)
        idx = self._index(local)
        for season in self.seasons:
            if season.contains(local.date()):
                return season.prices.get(idx, self.prices[idx])
        return self.prices[idx]

    def _index(self, local: datetime) -> int:
        quarter = (local.hour * 60 + local.minute) // 15
        return self.table[local.weekday()][quarter]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_time(raw: Any, what: str, errors: list[str], *, allow_24: bool) -> int | None:
    """``"HH:MM"`` → quarter index (0..96)."""
    text = str(raw).strip() if raw is not None else ""
    try:
        hh, mm = text.split(":")
        hours, minutes = int(hh), int(mm)
    except ValueError:
        errors.append(f"{what}: {raw!r} is not a time (HH:MM)")
        return None
    if (hours, minutes) == (24, 0) and allow_24:
        return _SLOTS_PER_DAY
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        errors.append(f"{what}: {raw!r} is not a time (HH:MM)")
        return None
    if minutes % 15:
        errors.append(f"{what}: {raw!r} must be on a quarter hour (:00, :15, :30, :45)")
        return None
    return (hours * 60 + minutes) // 15


def _parse_days(raw: Any, what: str, errors: list[str]) -> list[int] | None:
    """``[mon-fri, sun]`` (or one such string) → weekday numbers; ``None`` = all days."""
    if raw is None:
        return list(range(7))
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        errors.append(f"{what}: days must be a list such as [mon-fri, sat]")
        return None
    days: list[int] = []
    for item in items:
        token = str(item).strip().lower()
        first, _, last = token.partition("-")
        if first not in DAYS or (last and last not in DAYS):
            errors.append(f"{what}: unknown day {item!r} (use mon … sun or a range like mon-fri)")
            return None
        a = DAYS.index(first)
        b = DAYS.index(last) if last else a
        span = (b - a) % 7
        days.extend((a + i) % 7 for i in range(span + 1))
    return sorted(set(days))


def _window_slots(win: Any, what: str, errors: list[str]) -> list[tuple[int, int]]:
    """``(weekday, quarter)`` pairs a window covers."""
    if not isinstance(win, Mapping):
        errors.append(f"{what}: must be a mapping with days and from/to (or all_day)")
        return []
    n = len(errors)
    days = _parse_days(win.get("days"), what, errors)
    if win.get("all_day"):
        if "from" in win or "to" in win:
            errors.append(f"{what}: all_day and from/to cannot be combined")
        quarters = list(range(_SLOTS_PER_DAY))
    else:
        if "from" not in win or "to" not in win:
            errors.append(f"{what}: needs from and to (or all_day: true)")
            return []
        start = _parse_time(win["from"], f"{what}: from", errors, allow_24=False)
        end = _parse_time(win["to"], f"{what}: to", errors, allow_24=True)
        if start is None or end is None:
            return []
        if start == end % _SLOTS_PER_DAY:
            errors.append(f"{what}: from and to are equal (use all_day: true for a whole day)")
            return []
        if start < end:
            quarters = list(range(start, end))
        else:  # over midnight: the end and the start of the same day
            quarters = [*range(end), *range(start, _SLOTS_PER_DAY)]
    if len(errors) > n or days is None:
        return []
    return [(d, q) for d in days for q in quarters]


def _parse_month_day(raw: Any, what: str, errors: list[str]) -> tuple[int, int] | None:
    text = str(raw).strip() if raw is not None else ""
    try:
        month, day = (int(p) for p in text.split("-"))
        date(2000, month, day)  # leap year: 02-29 is allowed
    except ValueError:
        errors.append(f"{what}: {raw!r} is not a date (MM-DD)")
        return None
    return month, day


def _fmt_slot(day: int, quarter: int) -> str:
    minutes = quarter * 15
    return f"{DAYS[day]} {minutes // 60:02d}:{minutes % 60:02d}"


def _fmt_slots(slots: list[tuple[int, int]]) -> str:
    """A few covered quarters, for an error message."""
    shown = ", ".join(_fmt_slot(d, q) for d, q in slots[:3])
    return shown + (f" (+{len(slots) - 3} more quarter hours)" if len(slots) > 3 else "")


def parse_schedule(cfg: Mapping[str, Any]) -> tuple[TouSchedule | None, list[str]]:
    """Parse the ``timezone`` / ``periods`` / ``seasons`` of a ToU tariff."""
    errors: list[str] = []

    tz_name = str(cfg.get("timezone") or "Europe/Berlin")
    tz: ZoneInfo | None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"timezone: unknown timezone {tz_name!r}")
        tz = None

    periods = cfg.get("periods")
    if not isinstance(periods, list) or not periods:
        return None, [*errors, "periods: at least one period is required"]

    names: list[str] = []
    prices: list[float] = []
    defaults: list[int] = []
    table: list[list[int | None]] = [[None] * _SLOTS_PER_DAY for _ in range(7)]
    overlaps: dict[tuple[str, str], list[tuple[int, int]]] = {}

    for i, period in enumerate(periods):
        where = f"periods[{i + 1}]"
        if not isinstance(period, Mapping):
            errors.append(f"{where}: must be a mapping with name and price_eur_per_kwh")
            continue
        name = str(period.get("name") or "").strip()
        if not name:
            errors.append(f"{where}: name is required")
            name = f"#{i + 1}"
        elif name in names:
            errors.append(f"{where}: period name {name!r} is used twice")
        where = f"period {name!r}"
        price = period.get("price_eur_per_kwh")
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            errors.append(f"{where}: price_eur_per_kwh is required")
            price = 0.0
        idx = len(names)
        names.append(name)
        prices.append(float(price))
        if period.get("default"):
            defaults.append(idx)
        windows = period.get("windows") or []
        if not isinstance(windows, list):
            errors.append(f"{where}: windows must be a list")
            continue
        if not windows and not period.get("default"):
            errors.append(f"{where}: needs windows (or default: true)")
        for w, win in enumerate(windows):
            for day, q in _window_slots(win, f"{where}: windows[{w + 1}]", errors):
                other = table[day][q]
                if other is None:
                    table[day][q] = idx
                elif other != idx:
                    overlaps.setdefault((names[other], name), []).append((day, q))

    for (a, b), slots in overlaps.items():
        errors.append(f"periods {a!r} and {b!r} overlap: {_fmt_slots(slots)}")

    if len(defaults) > 1:
        errors.append("periods: only one period can be the default, found: "
                      + ", ".join(names[i] for i in defaults))
    elif defaults:
        for day in table:
            for q, cell in enumerate(day):
                if cell is None:
                    day[q] = defaults[0]
    else:
        gaps = [(d, q) for d in range(7) for q in range(_SLOTS_PER_DAY) if table[d][q] is None]
        if gaps:
            errors.append(f"periods: these times are not covered by any window: {_fmt_slots(gaps)}"
                          " (add the missing windows or mark one period default: true)")

    seasons = _parse_seasons(cfg.get("seasons"), names, errors)

    if errors or tz is None:
        return None, errors
    return TouSchedule(
        tz=tz,
        names=tuple(names),
        prices=tuple(prices),
        table=tuple(tuple(c for c in day if c is not None) for day in table),
        seasons=tuple(seasons),
    ), []


def _parse_seasons(raw: Any, names: list[str], errors: list[str]) -> list[Season]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("seasons: must be a list")
        return []
    seasons: list[Season] = []
    for i, item in enumerate(raw):
        where = f"seasons[{i + 1}]"
        if not isinstance(item, Mapping):
            errors.append(f"{where}: must be a mapping with from, to and periods")
            continue
        start = _parse_month_day(item.get("from"), f"{where}: from", errors)
        end = _parse_month_day(item.get("to"), f"{where}: to", errors)
        overrides = item.get("periods")
        if not isinstance(overrides, Mapping) or not overrides:
            errors.append(f"{where}: periods must map period names to prices, e.g. {{day: 0.34}}")
            continue
        prices: dict[int, float] = {}
        for name, price in overrides.items():
            if name not in names:
                errors.append(f"{where}: unknown period {name!r} (one of {', '.join(names)})")
            elif not isinstance(price, (int, float)) or isinstance(price, bool):
                errors.append(f"{where}: price of {name!r} must be a number, got {price!r}")
            else:
                prices[names.index(name)] = float(price)
        if start is not None and end is not None:
            seasons.append(Season(start, end, prices))

    # Overlapping seasons would make the price depend on their order.
    year = [date(2000, 1, 1) + timedelta(days=n) for n in range(366)]
    for a in range(len(seasons)):
        for b in range(a + 1, len(seasons)):
            shared = [d for d in year if seasons[a].contains(d) and seasons[b].contains(d)]
            if shared:
                errors.append(f"seasons {a + 1} and {b + 1} overlap (e.g. on "
                              f"{shared[0].month:02d}-{shared[0].day:02d})")
    return seasons

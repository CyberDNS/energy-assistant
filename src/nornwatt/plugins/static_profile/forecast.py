"""StaticProfileForecast — daily consumption profile for consumer devices."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ...core.models import ForecastPoint, ForecastQuantity

# Map weekday int (Monday=0) → name string
_WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


def _normalize_profile(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Accept both list-of-dicts and plain-dict profile formats.

    List format (YAML compact-notation, standard in config.yaml)::

        profile:
          - weekdays:
            - hour: 0
              consumed_kwh: 0.5

    Dict format (alternative)::

        profile:
          weekdays:
            - hour: 0
              consumed_kwh: 0.5
    """
    if isinstance(raw, dict):
        return raw  # type: ignore[return-value]
    if isinstance(raw, list):
        result: dict[str, list[dict[str, Any]]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                if isinstance(value, list):
                    result[key] = value
        return result
    return {}


class StaticProfileForecast:
    """Consumption forecast built from a static time-of-day / day-of-week profile.

    The profile defines energy consumption for each time segment of a day.
    Each segment entry has:

    * ``hour``          — local hour at which this segment begins (0–23).
    * ``consumed_kwh``  — total kWh consumed from this ``hour`` until the
                          next entry's ``hour`` (or midnight for the last
                          entry).

    Power (kW) for each segment is computed as::

        power_kw = consumed_kwh / duration_hours

    Supported day-type keys: ``weekdays``, ``weekends``, ``monday`` …
    ``sunday``.  Individual weekday names take priority over the group
    keys ``weekdays`` / ``weekends``.

    Example — heatpump with different weekday/weekend consumption::

        profile:
          weekdays:
            - hour: 0
              consumed_kwh: 0.5    # 00–06 h, 6 h → 0.083 kW
            - hour: 6
              consumed_kwh: 1.5    # 06–09 h, 3 h → 0.500 kW
            - hour: 9
              consumed_kwh: 0.8    # 09–17 h, 8 h → 0.100 kW
            - hour: 17
              consumed_kwh: 2.5    # 17–22 h, 5 h → 0.500 kW
            - hour: 22
              consumed_kwh: 0.5    # 22–00 h, 2 h → 0.250 kW
          weekends:
            - hour: 0
              consumed_kwh: 10     # full day, 24 h → 0.417 kW

    Single-entry shortcut — constant load all day::

        profile:
          weekdays:
            - hour: 0
              consumed_kwh: 24.0   # → 1.0 kW constant
    """

    def __init__(self, profile: Any) -> None:
        normalized = _normalize_profile(profile)
        # Precompute: for each day type, a sorted list of (start_hour, power_kw)
        self._segments: dict[str, list[tuple[int, float]]] = {}
        for day_type, entries in normalized.items():
            sorted_entries = sorted(entries, key=lambda e: int(e["hour"]))
            segs: list[tuple[int, float]] = []
            for i, entry in enumerate(sorted_entries):
                start_h = int(entry["hour"])
                end_h = (
                    int(sorted_entries[i + 1]["hour"])
                    if i + 1 < len(sorted_entries)
                    else 24
                )
                duration_h = end_h - start_h
                kwh = float(entry["consumed_kwh"])
                power_kw = kwh / duration_h if duration_h > 0 else 0.0
                segs.append((start_h, power_kw))
            self._segments[day_type] = segs

    @property
    def quantity(self) -> ForecastQuantity:
        return ForecastQuantity.CONSUMPTION

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Return hourly ForecastPoints from now to now + horizon."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        # +1 for a lookahead point so _align() can interpolate the last step
        n_hours = max(1, int(horizon.total_seconds() / 3600)) + 1
        return [
            ForecastPoint(
                timestamp=now + timedelta(hours=h),
                value=self._power_at(now + timedelta(hours=h)),
            )
            for h in range(n_hours)
        ]

    def _power_at(self, ts: datetime) -> float:
        """Return the consumption power (kW) for a given UTC timestamp."""
        local_ts = ts.astimezone()
        return self._power_for(local_ts.weekday(), local_ts.hour)

    def _power_for(self, weekday: int, hour: int) -> float:
        """Return the consumption power (kW) for a weekday (0=Mon) and local hour.

        Exposed separately so tests can call it without timezone complications.
        """
        # Try keys in specificity order: specific day > weekdays/weekends
        day_name = _WEEKDAY_NAMES[weekday]
        group_name = "weekdays" if weekday < 5 else "weekends"
        for key in (day_name, group_name):
            if key in self._segments:
                segs = self._segments[key]
                # Step-function: last segment whose start_hour ≤ current hour
                power = segs[0][1]
                for seg_hour, seg_power in segs:
                    if hour >= seg_hour:
                        power = seg_power
                return power

        # Fallback: use first available day type's profile
        if self._segments:
            segs = next(iter(self._segments.values()))
            power = segs[0][1]
            for seg_hour, seg_power in segs:
                if hour >= seg_hour:
                    power = seg_power
            return power

        return 0.0

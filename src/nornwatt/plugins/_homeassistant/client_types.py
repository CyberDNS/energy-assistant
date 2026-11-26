"""Types shared by the Home Assistant REST client and the websocket cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def parse_ts(value: Any) -> datetime | None:
    """Parse a timestamp as HA returns it: ISO string or epoch (s or ms)."""
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            return None
        return ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    return None


@dataclass(frozen=True)
class EntityState:
    """One entity's state."""

    state: str | None
    attributes: dict[str, Any]
    last_changed: datetime | None
    last_updated: datetime | None
    last_reported: datetime | None
    """When the integration last reported a value, even an unchanged one
    (HA 2024.3+).  Falls back to ``last_updated`` on older versions and in
    the websocket cache (the websocket does not report it)."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> EntityState:
        updated = parse_ts(data.get("last_updated"))
        return cls(
            state=data.get("state"),
            attributes=dict(data.get("attributes") or {}),
            last_changed=parse_ts(data.get("last_changed")),
            last_updated=updated,
            last_reported=parse_ts(data.get("last_reported")) or updated,
        )

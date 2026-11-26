"""ChangeGate — log/act on a value only when it differs from the last call.

Control operations run on every control-loop tick (every ``control_interval_s``
seconds) even when the commanded setpoint hasn't moved. Logging on every tick
drowns real state transitions in repeats. A ``ChangeGate`` remembers the last
value seen per key so callers can gate a log line (or any other side effect)
on an actual change.
"""

from __future__ import annotations

from typing import Any, Hashable

_UNSET = object()


class ChangeGate:
    """Tracks the last value seen per key and reports whether it changed."""

    def __init__(self) -> None:
        self._last: dict[Hashable, Any] = {}

    def changed(self, key: Hashable, value: Any) -> bool:
        """Return True and remember *value* if it differs from the last call for *key*."""
        previous = self._last.get(key, _UNSET)
        if previous == value:
            return False
        self._last[key] = value
        return True

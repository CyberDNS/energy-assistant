"""Actuators: writing a value to a Home Assistant entity.

One implementation for every Home Assistant device type:

``SwitchActuator``   on/off — ``switch``, ``input_boolean``, ``light``, ``fan``
``NumberActuator``   a number — ``number``, ``input_number``
``SelectActuator``   an option — ``select``, ``input_select``
``ActionActuator``   any action (script, service) with the value as a variable

Each ``apply(desired)``

* **reconciles**: reads the entity first and only writes on a mismatch
  (numbers within a tolerance count as equal), so a control tick that asks
  for the same value again sends nothing, and a value someone (or the device)
  changed is put back;
* **rate-limits**: after a write, the same value is not written again for
  ``min_interval_s`` — Home Assistant and the device get time to apply it;
* **checks the read-back**: when the entity still shows another value
  ``follow_timeout_s`` after the write, the actuator reports it as not
  followed (``health``) and logs one warning.  Health checks (#67) use it.

If the entity cannot be read, the value is written anyway.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from ...core.device_model import DeviceConfigError, ParamSpec

_log = logging.getLogger(__name__)

_GONE = frozenset({"unavailable", "unknown", "none", ""})

SWITCH_DOMAINS = ("switch", "input_boolean", "light", "fan")
NUMBER_DOMAINS = ("number", "input_number")
SELECT_DOMAINS = ("select", "input_select")


@dataclass(frozen=True)
class ActuatorHealth:
    """Whether the entity follows what was written."""

    entity_id: str
    desired: Any = None
    current: Any = None
    followed: bool = True
    since: float | None = None
    """Monotonic time the mismatch was first seen after the timeout."""

    def to_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "desired": self.desired,
                "current": self.current, "followed": self.followed}


class Actuator:
    """Base class: reconcile, rate limit and read-back check (see module doc)."""

    domains: tuple[str, ...] = ()

    def __init__(
        self,
        where: str,
        client: Any,
        entity_id: str,
        *,
        min_interval_s: float = 5.0,
        follow_timeout_s: float = 60.0,
    ) -> None:
        self.where = where
        self._client = client
        self.entity_id = entity_id
        self.min_interval_s = min_interval_s
        self.follow_timeout_s = follow_timeout_s
        self._written: Any = None
        self._written_at: float | None = None
        """Last write (rate limit)."""
        self._first_written_at: float | None = None
        """First write of the current value (read-back check)."""
        self._health = ActuatorHealth(entity_id)
        self._clock = time.monotonic

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def health(self) -> ActuatorHealth:
        return self._health

    # -- to implement -----------------------------------------------------

    def _normalize(self, desired: Any, state: Any) -> Any:
        """The value that will actually be written (clamped, rounded, …)."""
        return desired

    def _current(self, state: Any) -> Any:
        """The entity's current value from its ``EntityState`` (None = unknown)."""
        raise NotImplementedError

    def _matches(self, desired: Any, current: Any) -> bool:
        return desired == current

    async def _write(self, value: Any) -> None:
        raise NotImplementedError

    # -- the protocol ------------------------------------------------------

    async def _read(self) -> Any:
        try:
            return await self._client.get_entity(self.entity_id)
        except Exception as exc:  # noqa: BLE001 — unknown: write unconditionally
            _log.debug("%s: reading %s failed: %s", self.where, self.entity_id, exc)
            return None

    async def apply(self, desired: Any) -> bool:
        """Bring the entity to *desired*.  Returns True when it wrote."""
        state = await self._read()
        value = self._normalize(desired, state)
        current = self._current(state) if state is not None else None
        now = self._clock()
        if current is not None and self._matches(value, current):
            if not self._health.followed:
                _log.info("%s: %s follows again (%r)", self.where, self.entity_id, current)
            self._health = ActuatorHealth(self.entity_id, value, current)
            self._written = None  # a later mismatch starts a new write and check
            return False
        same = self._written is not None and self._matches(value, self._written)
        if same and self._written_at is not None:
            assert self._first_written_at is not None
            if now - self._first_written_at >= self.follow_timeout_s and current is not None:
                self._report_not_followed(value, current, now)
            if now - self._written_at < self.min_interval_s:
                return False
        try:
            await self._write(value)
        except Exception:
            _log.warning("%s: writing %r to %s failed", self.where, value, self.entity_id,
                         exc_info=True)
            raise
        if not same:
            self._health = ActuatorHealth(self.entity_id, value, current)
            self._first_written_at = now
        self._written, self._written_at = value, now
        _log.info("%s: %s → %r (was %r)", self.where, self.entity_id, value, current)
        return True

    def _report_not_followed(self, value: Any, current: Any, now: float) -> None:
        if self._health.followed:
            _log.warning("%s: %s does not follow: wrote %r, it shows %r after %.0f s",
                         self.where, self.entity_id, value, current, self.follow_timeout_s)
            self._health = ActuatorHealth(self.entity_id, value, current, False, now)
        else:
            self._health = ActuatorHealth(self.entity_id, value, current, False,
                                          self._health.since)


class SwitchActuator(Actuator):
    """On/off through ``homeassistant.turn_on`` / ``turn_off``."""

    domains = SWITCH_DOMAINS

    def _normalize(self, desired: Any, state: Any) -> bool:
        return bool(desired)

    def _current(self, state: Any) -> bool | None:
        raw = (state.state or "").lower()
        if raw in _GONE:
            return None
        return raw == "on"

    async def _write(self, value: bool) -> None:
        service = "turn_on" if value else "turn_off"
        await self._client.call_service("homeassistant", service, {"entity_id": self.entity_id})


#: unit_of_measurement → W, for ``NumberActuator(power=True)``.
_POWER_UNITS = {"w": 1.0, "kw": 1000.0, "mw": 1e6}


class NumberActuator(Actuator):
    """A number entity (``number.set_value``), clamped to its min / max and step.

    ``power=True``: ``apply`` takes watts and converts them to the entity's
    unit (W or kW); the tolerance is in W too.
    """

    domains = NUMBER_DOMAINS

    def __init__(
        self, where: str, client: Any, entity_id: str, *,
        tolerance: float = 0.5, minimum: float | None = None, maximum: float | None = None,
        power: bool = False, **kwargs: Any,
    ) -> None:
        super().__init__(where, client, entity_id, **kwargs)
        self.tolerance = tolerance
        self.minimum = minimum
        self.maximum = maximum
        self.power = power
        self._factor = 1.0  # W per entity unit (power numbers)

    def _normalize(self, desired: Any, state: Any) -> float:
        value = float(desired)
        attrs = state.attributes if state is not None else {}
        if self.power:
            unit = str(attrs.get("unit_of_measurement") or "W").strip().lower()
            self._factor = _POWER_UNITS.get(unit, 1.0)
            value /= self._factor
        lo = _first_number(self.minimum, attrs.get("min"))
        hi = _first_number(self.maximum, attrs.get("max"))
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        step = _first_number(attrs.get("step"))
        if step and step > 0:
            base = lo or 0.0
            value = base + round((value - base) / step) * step
        return round(value, 6)

    def _current(self, state: Any) -> float | None:
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(value) else value

    def _matches(self, desired: Any, current: Any) -> bool:
        return abs(float(desired) - float(current)) * self._factor <= self.tolerance

    async def _write(self, value: float) -> None:
        await self._client.call_service(self.domain, "set_value",
                                        {"entity_id": self.entity_id, "value": value})


class SelectActuator(Actuator):
    """An option of a select entity (``select.select_option``)."""

    domains = SELECT_DOMAINS

    def __init__(self, where: str, client: Any, entity_id: str, **kwargs: Any) -> None:
        super().__init__(where, client, entity_id, **kwargs)
        self._checked_options = False

    def _normalize(self, desired: Any, state: Any) -> str:
        value = str(desired)
        options = state.attributes.get("options") if state is not None else None
        if options and not self._checked_options:
            self._checked_options = True
            if value not in options:
                _log.warning("%s: %r is not an option of %s (options: %s)", self.where,
                             value, self.entity_id, ", ".join(map(str, options)))
        return value

    def _current(self, state: Any) -> str | None:
        raw = state.state
        return None if raw is None or raw.lower() in _GONE else str(raw)

    async def _write(self, value: str) -> None:
        await self._client.call_service(self.domain, "select_option",
                                        {"entity_id": self.entity_id, "option": value})


class ActionActuator:
    """Calls an action (``script.charge_battery``, ``modbus.write_register``,
    …) with the value in its data.

    An action has no state to read back: it is called when the value
    differs from the last call by more than *tolerance*, and again every
    *refresh_s* (the device or an integration restart may have lost it).
    """

    def __init__(
        self, where: str, client: Any, action: str, *,
        data: dict[str, Any] | None = None, value_key: str = "power_w",
        tolerance: float = 25.0, refresh_s: float = 60.0,
    ) -> None:
        domain, dot, service = action.partition(".")
        if not dot or not domain or not service:
            raise DeviceConfigError([f"{where}: action must be like script.name, got {action!r}"])
        self.where = where
        self._client = client
        self.domain, self.service = domain, service
        self.data = dict(data or {})
        self.value_key = value_key
        self.tolerance = tolerance
        self.refresh_s = refresh_s
        self._last: float | None = None
        self._last_at: float | None = None
        self._clock = time.monotonic

    @property
    def entity_id(self) -> str:
        return f"{self.domain}.{self.service}"

    @property
    def health(self) -> ActuatorHealth:
        return ActuatorHealth(self.entity_id, self._last, None)

    async def apply(self, desired: Any) -> bool:
        value = float(desired)
        now = self._clock()
        fresh = self._last_at is not None and now - self._last_at < self.refresh_s
        if fresh and self._last is not None and abs(value - self._last) <= self.tolerance:
            return False
        await self._client.call_service(self.domain, self.service,
                                        {**self.data, self.value_key: value})
        if self._last is None or abs(value - self._last) > self.tolerance:
            _log.info("%s: %s(%s=%r)", self.where, self.entity_id, self.value_key, value)
        self._last, self._last_at = value, now
        return True


def _first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


# ---------------------------------------------------------------------------
# Binding specs and parsing
# ---------------------------------------------------------------------------

SWITCH_BINDING: dict[str, ParamSpec] = {
    "entity": ParamSpec(str, required=True, entity=True, domains=SWITCH_DOMAINS,
                        label="Entity", description="Entity switched on / off"),
}


def parse_switch(where: str, client: Any, raw: Any) -> SwitchActuator:
    """A ``SwitchActuator`` from a ``{entity: switch.…}`` binding."""
    entity = raw.get("entity") if isinstance(raw, dict) else None
    if not isinstance(entity, str) or not entity:
        raise DeviceConfigError([f"{where}: needs 'entity: <switch entity>'"])
    unknown = set(raw) - set(SWITCH_BINDING)
    if unknown:
        raise DeviceConfigError([(f"{where}: unknown key(s) {', '.join(sorted(unknown))} "
                                  "(allowed: entity)")])
    return SwitchActuator(where, client, entity)

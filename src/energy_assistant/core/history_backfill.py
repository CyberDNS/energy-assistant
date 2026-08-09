"""One-time backfill of local measurement/signal history from external
sources that expose queryable history.

Local storage (the ``measurements``/``signals`` SQLite tables) is what makes
the rest of the platform backend-agnostic — every device, regardless of
whether it's HA-, ioBroker-, or MQTT-backed, already writes into it via the
same ``get_state()`` → poll loop path. This module doesn't replace that; it
just seeds it once at startup so learned models don't have to wait days of
live polling to see every hour-of-day/weekday combination.

Today only Home Assistant exposes a history API we can call
(``HAClient.get_history``, via the recorder's ``/api/history/period`` REST
endpoint). ioBroker devices have no history source implemented yet — they're
simply skipped here and continue to cold-start via live polling, same as
before this module existed. When ioBroker (or another backend) gains a
history API, add a resolution branch in ``resolve_history_source`` — no
other code here needs to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Union

from .config import AppConfig
from .learned_model import nearest_value
from .learned_model_store import LearnedModelStore
from .models import Measurement

_log = logging.getLogger(__name__)

_MAX_JOIN_GAP = timedelta(minutes=30)
# How close the earliest existing local point must be to the requested
# backfill horizon to consider that horizon "already covered" — a few hours
# of slack for poll-loop jitter around the actual backfill start.
_ALREADY_BACKFILLED_TOLERANCE = timedelta(hours=6)


def _already_covers_horizon(existing: list[Any], start: datetime) -> bool:
    """Return True if *existing* rows already reach back close to *start*.

    Used instead of a plain "any rows exist" check: local storage
    accumulates from live polling regardless of backfill, so a handful of
    recent rows (e.g. from an hour of live polling since the feature was
    added, or a reused dev DB across restarts) must not be mistaken for a
    completed backfill and block a real one from ever running.
    """
    if not existing:
        return False
    earliest = min(row.timestamp for row in existing)
    return earliest <= start + _ALREADY_BACKFILLED_TOLERANCE


@dataclass(frozen=True)
class HaEntitySource:
    """A single Home Assistant power entity."""

    entity_id: str
    invert_sign: bool = False


@dataclass(frozen=True)
class DifferentialSource:
    """A derived series = minuend − subtrahend, each itself a HistorySource."""

    minuend: "HistorySource"
    subtrahend: "HistorySource"
    min_power_w: float | None = None
    max_power_w: float | None = None


HistorySource = Union[HaEntitySource, DifferentialSource]


def resolve_history_source(device_id: str, app_config: AppConfig) -> HistorySource | None:
    """Return a ``HistorySource`` describing how to backfill *device_id*'s
    power history, or ``None`` if no history source is available for it.

    Recurses through ``differential`` devices; returns ``None`` as soon as
    any leg is backed by something without a history source (currently:
    anything other than a single-entity ``generic_homeassistant`` device).
    """
    cfg = app_config.devices.get(device_id)
    if not cfg:
        return None
    type_name = cfg.get("type")

    if type_name == "generic_homeassistant":
        entity_id = cfg.get("oid_power") or cfg.get("power")
        if not entity_id:
            # import/export-pair mode isn't supported for backfill (unused
            # in practice today) — cold-starts via live polling instead.
            return None
        return HaEntitySource(entity_id=entity_id, invert_sign=bool(cfg.get("invert_sign", False)))

    if type_name == "differential":
        if cfg.get("minuend_field", "power_w") != "power_w" or cfg.get("subtrahend_field", "power_w") != "power_w":
            return None  # only the default power_w field is supported for backfill
        minuend_id = cfg.get("minuend")
        subtrahend_id = cfg.get("subtrahend")
        if not minuend_id or not subtrahend_id:
            return None
        minuend_src = resolve_history_source(minuend_id, app_config)
        subtrahend_src = resolve_history_source(subtrahend_id, app_config)
        if minuend_src is None or subtrahend_src is None:
            return None
        min_w = cfg.get("min_w")
        max_w = cfg.get("max_w")
        return DifferentialSource(
            minuend=minuend_src,
            subtrahend=subtrahend_src,
            min_power_w=float(min_w) if min_w is not None else None,
            max_power_w=float(max_w) if max_w is not None else None,
        )

    # No history source implemented for this backend yet (e.g. ioBroker).
    return None


def _to_float_points(raw: list[tuple[datetime, str]]) -> list[tuple[datetime, float]]:
    result = []
    for ts, state in raw:
        try:
            result.append((ts, float(state)))
        except (TypeError, ValueError):
            continue  # skip "unavailable" / "unknown" / non-numeric states
    return result


def _combine_differential(
    minuend_pts: list[tuple[datetime, float]],
    subtrahend_pts: list[tuple[datetime, float]],
    min_power_w: float | None,
    max_power_w: float | None,
) -> list[tuple[datetime, float]]:
    if not minuend_pts or not subtrahend_pts:
        return []
    sorted_subtrahend = sorted(subtrahend_pts, key=lambda p: p[0])
    result: list[tuple[datetime, float]] = []
    for ts, m_val in minuend_pts:
        s_val = nearest_value(sorted_subtrahend, ts, _MAX_JOIN_GAP)
        if s_val is None:
            continue
        power = m_val - s_val
        if min_power_w is not None:
            power = max(power, min_power_w)
        if max_power_w is not None:
            power = min(power, max_power_w)
        result.append((ts, power))
    return result


async def fetch_series(
    source: HistorySource, ha_client: Any, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Fetch and, for derived sources, combine the historical power series."""
    if isinstance(source, HaEntitySource):
        raw = await ha_client.get_history(source.entity_id, start, end)
        points = _to_float_points(raw)
        if source.invert_sign:
            points = [(ts, -v) for ts, v in points]
        return points

    minuend_pts = await fetch_series(source.minuend, ha_client, start, end)
    subtrahend_pts = await fetch_series(source.subtrahend, ha_client, start, end)
    return _combine_differential(minuend_pts, subtrahend_pts, source.min_power_w, source.max_power_w)


def _merge_presence(histories: list[list[tuple[datetime, str]]]) -> list[tuple[datetime, float]]:
    """OR-combine several person entities' step-function histories into a
    single anyone-home (0/1) series, using last-known-state carry-forward."""
    events: list[tuple[datetime, int, bool]] = []
    for idx, hist in enumerate(histories):
        for ts, state in hist:
            events.append((ts, idx, state == "home"))
    events.sort(key=lambda e: e[0])

    last_state = [False] * len(histories)
    result: list[tuple[datetime, float]] = []
    for ts, idx, is_home in events:
        last_state[idx] = is_home
        result.append((ts, 1.0 if any(last_state) else 0.0))
    return result


async def run_history_backfill(
    app_config: AppConfig,
    ha_client: Any,
    storage: Any,
    learned_model_store: LearnedModelStore,
    backfill_days: int = 10,
) -> None:
    """Seed local measurement/signal history from HA, once, for whatever
    devices/signals don't already have history reaching back to the backfill
    horizon.

    Idempotent: a device_id/signal_id whose earliest local row already
    reaches back near *start* is left untouched (a real backfill already
    ran, or enough live polling has accumulated on its own). A handful of
    recent-only rows — e.g. from live polling before a backfill has ever
    run, or a dev DB reused across restarts — does NOT count as covered.
    """
    if ha_client is None:
        _log.info("history backfill: no HA client configured — skipping")
        return

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=backfill_days)
    env = app_config.environment

    # Backfill is a best-effort startup enhancement, not a hard dependency —
    # a slow/unreachable HA history call (timeout, HTTP error, etc.) for one
    # signal/device must not take the rest of startup down with it.

    temp_entity = env.get("outdoor_temperature")
    if temp_entity and not _already_covers_horizon(
        await storage.query_signals("outdoor_temperature", start, now), start
    ):
        try:
            raw = await ha_client.get_history(temp_entity, start, now)
            points = _to_float_points(raw)
            for ts, value in points:
                await storage.write_signal("outdoor_temperature", ts, value)
            _log.info("history backfill: outdoor_temperature — %d points", len(points))
        except Exception as exc:  # noqa: BLE001
            _log.warning("history backfill: outdoor_temperature failed (%s) — skipping", exc)

    person_entities = env.get("presence") or []
    if person_entities and not _already_covers_horizon(
        await storage.query_signals("anyone_home", start, now), start
    ):
        try:
            histories = [await ha_client.get_history(e, start, now) for e in person_entities]
            merged = _merge_presence(histories)
            for ts, value in merged:
                await storage.write_signal("anyone_home", ts, value)
            _log.info("history backfill: anyone_home — %d points", len(merged))
        except Exception as exc:  # noqa: BLE001
            _log.warning("history backfill: anyone_home failed (%s) — skipping", exc)

    for device_id in learned_model_store.configured_device_ids():
        if _already_covers_horizon(await storage.query(device_id, start, now), start):
            continue  # already has history reaching back to the horizon
        source = resolve_history_source(device_id, app_config)
        if source is None:
            _log.info(
                "history backfill: no history source for %r (likely "
                "ioBroker-backed, or a differential with an ioBroker leg) — "
                "will cold-start from live polling instead",
                device_id,
            )
            continue
        try:
            points = await fetch_series(source, ha_client, start, now)
            for ts, power_w in points:
                await storage.write(Measurement(device_id=device_id, timestamp=ts, power_w=power_w))
            _log.info("history backfill: %r — %d points", device_id, len(points))
        except Exception as exc:  # noqa: BLE001
            _log.warning("history backfill: %r failed (%s) — will cold-start from live polling", device_id, exc)

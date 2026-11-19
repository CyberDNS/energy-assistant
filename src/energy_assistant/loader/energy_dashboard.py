"""Import the consumers of the Home Assistant Energy dashboard as devices.

Opt-in with a top-level ``energy_dashboard:`` section::

    energy_dashboard:
      import_consumers: true
      # Leave out entries that are modelled otherwise (EV chargers, the heat
      # pump, controllable loads).  Matches the energy sensor, the power
      # sensor, the name shown in HA or the generated device id.
      exclude:
        - sensor.chargepoint_bella_energy
        - Banzert
      tariff: household          # optional; default: the default tariff
      learning:                  # optional extra options for the learned forecasts
        history_days: 365

Every "Individual device" of the Energy dashboard becomes a consumer:

* with a power sensor (``stat_rate``) a ``generic_homeassistant`` device that
  reads it live and learns its forecast from it (default learning, see
  ``forecast_config``);
* with only an energy meter, a virtual ``generic_consumer`` whose ``learned``
  forecast is trained on the meter's hourly consumption.

Entries whose sensors already appear anywhere in a configured device are
skipped, so a device you configured yourself always wins.  A device nested
under another one in the dashboard ("upstream device") is marked
``included_in`` its parent, and the parent learns its own load minus the
children's.

The dashboard configuration is only reachable over the WebSocket API.  The
last copy fetched is cached next to the database, so a restart while Home
Assistant is down keeps the same devices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from ..core.config import AppConfig

_log = logging.getLogger(__name__)

_FETCH_TIMEOUT_S = 20.0
CACHE_FILE = "energy_dashboard_prefs.json"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "consumer"


def _strings(value: Any) -> set[str]:
    """Every string anywhere inside *value* (dicts, lists, scalars)."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(_strings(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_strings(v) for v in value)) if value else set()
    return set()


def add_dashboard_consumers(
    app_config: AppConfig, prefs: dict[str, Any], options: dict[str, Any]
) -> list[str]:
    """Add the dashboard's consumers from *prefs* to ``app_config.devices``.

    Returns the ids of the devices added.
    """
    exclude = {str(e).lower() for e in options.get("exclude") or []}
    learning = options.get("learning", True)
    learn_opts = dict(learning) if isinstance(learning, dict) else {}
    tariff = options.get("tariff")

    used: dict[str, str] = {}
    for device_id, cfg in app_config.devices.items():
        for s in _strings(cfg):
            used.setdefault(s, device_id)

    added: dict[str, dict[str, Any]] = {}   # device id → cfg
    by_stat: dict[str, str] = {}             # stat_consumption → device id
    entries: list[dict[str, Any]] = []
    for entry in prefs.get("device_consumption") or []:
        stat = entry.get("stat_consumption")
        if not stat:
            continue
        rate = entry.get("stat_rate") or None
        name = str(entry.get("name") or stat)
        slug = _slug(name)
        keys = {stat.lower(), (rate or "").lower(), name.lower(), slug}
        if keys & exclude:
            _log.info("Energy dashboard: %s (%s) excluded", name, stat)
            continue
        owner = used.get(stat) or (used.get(rate) if rate else None)
        if owner is not None:
            _log.info("Energy dashboard: %s (%s) is already configured as %r", name, stat, owner)
            continue
        if rate is None and learning is False:
            # Nothing to read live and nothing to forecast.
            continue

        device_id = slug
        n = 2
        while device_id in app_config.devices or device_id in added:
            device_id = f"{slug}_{n}"
            n += 1

        cfg: dict[str, Any] = {"role": "consumer", "label": name, "energy_dashboard": stat}
        if tariff:
            cfg["tariff"] = tariff
        if rate is not None:
            cfg.update(type="generic_homeassistant", oid_power=rate)
            cfg["learning"] = dict(learn_opts) if learning is not False else False
        else:
            cfg.update(
                type="generic_consumer",
                forecast={"type": "learned", "history_entity": stat, **learn_opts},
            )
        added[device_id] = cfg
        by_stat[stat] = device_id
        entries.append(entry)

    # Nesting: a child's load is inside its parent's meter.
    for entry in entries:
        parent_stat = entry.get("included_in_stat")
        if not parent_stat:
            continue
        child_id = by_stat[entry["stat_consumption"]]
        parent_id = by_stat.get(parent_stat)
        if parent_id is None:
            if parent_stat in used:
                _log.warning(
                    "Energy dashboard: %s is part of %r — its load may be counted twice; "
                    "exclude one of them", child_id, used[parent_stat],
                )
            continue
        child, parent = added[child_id], added[parent_id]
        child["included_in"] = parent_id
        child_entity = child.get("oid_power") or child["forecast"]["history_entity"]
        target = parent.get("forecast") or parent.get("learning")
        if isinstance(target, dict):
            target.setdefault("subtract", []).append(child_entity)

    app_config.devices.update(added)
    if added:
        _log.info(
            "Energy dashboard: imported %d consumers: %s", len(added), ", ".join(added)
        )
    return list(added)


async def import_energy_dashboard(
    app_config: AppConfig, ha_client: Any, data_dir: Path | None = None
) -> list[str]:
    """Fetch the Energy dashboard and add its consumers (if enabled).

    Never raises: without Home Assistant (and without a cached copy) nothing
    is imported.
    """
    options = app_config.energy_dashboard or {}
    if not options.get("import_consumers"):
        return []
    if ha_client is None:
        _log.warning("energy_dashboard.import_consumers needs the homeassistant backend")
        return []

    cache = data_dir / CACHE_FILE if data_dir is not None else None
    try:
        prefs = await asyncio.wait_for(ha_client.get_energy_prefs(), _FETCH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        if cache is None or not cache.exists():
            _log.warning("Energy dashboard unavailable (%s) — no consumers imported", exc)
            return []
        _log.warning("Energy dashboard unavailable (%s) — using the cached copy", exc)
        try:
            prefs = json.loads(cache.read_text())
        except (OSError, ValueError) as cache_exc:
            _log.warning("Energy dashboard cache unreadable: %s", cache_exc)
            return []
    else:
        if cache is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(prefs, indent=2))
            except OSError as exc:
                _log.warning("Could not cache the energy dashboard: %s", exc)
    return add_dashboard_consumers(app_config, prefs, options)

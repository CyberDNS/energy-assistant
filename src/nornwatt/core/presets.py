"""Device presets: ready-made configurations for common Home Assistant integrations.

Each ``presets/<id>.yaml`` describes one device as it appears through one
integration::

    title: openWB chargepoint (openWB 2 integration)
    category: charger
    integration: openWB (MQTT)          # shown in the config UI
    url: https://…                      # the integration's page
    verified: true                      # checked against a real installation
    description: …                      # what to check after applying it
    variables:                          # filled in by the user
      prefix: { label: Entity prefix, example: openwbbox_chargepoint_5 }
    device:                             # the device entry, without id / label
      type: homeassistant_charger
      settings: { phases: 3 }           # the plugin's own settings
      capabilities: { … "{prefix}" … }  # "{name}" is replaced by the variable
      params: { … }

The config UI offers the presets of a category when a device is added, asks
for the variables and fills the form; nothing is applied without the user
saving the form.  ``tests/nornwatt/test_presets.py`` checks every
preset (with its example values) against the plugin and category schemas.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

PRESET_DIR = Path(__file__).resolve().parent.parent / "presets"
_VAR = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@cache
def load_presets() -> tuple[dict[str, Any], ...]:
    """Every preset, sorted by category and title (``id`` = the file name)."""
    out = []
    for path in sorted(PRESET_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["id"] = path.stem
        out.append(data)
    return tuple(sorted(out, key=lambda p: (p["category"], p["title"])))


def presets_json() -> list[dict[str, Any]]:
    """The presets as the config UI gets them (``GET /api/config/schema``)."""
    return [dict(p) for p in load_presets()]


def _fill(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _VAR.sub(lambda m: values.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {_fill(k, values): _fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    return value


def apply_preset(preset: dict[str, Any], values: dict[str, str] | None = None) -> dict[str, Any]:
    """The device entry of *preset* with its variables filled in (missing ones
    get their example).  ``settings`` are merged into the entry, as in
    ``config.yaml``."""
    filled = {name: str((values or {}).get(name) or spec.get("example", ""))
              for name, spec in (preset.get("variables") or {}).items()}
    device = _fill(preset["device"], filled)
    entry: dict[str, Any] = {"category": preset["category"], "type": device["type"]}
    entry.update(device.get("settings") or {})
    for key in ("capabilities", "params"):
        if device.get(key):
            entry[key] = device[key]
    return entry

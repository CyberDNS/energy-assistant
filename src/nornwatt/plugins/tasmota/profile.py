"""Tasmota field profiles — the value names of a known device or script, as data.

A profile describes the JSON one kind of Tasmota device reports: the standard
``ENERGY`` sensor of plugs, or the values a smart-meter script from the
Tasmota docs (Smart Meter Interface) defines.  Everything about one
installation (host, password, poll interval, …) stays in ``config.yaml``.

Profile file format (YAML)::

    name: "Iskra MT175 (SML)"
    description: >              # optional: which script / device it expects
      Script "Iskra MT 175 (SML)" from the Tasmota Smart Meter Interface docs.
    sensor: MT175               # sensor object below StatusSNS (the script's
                                # "+1,3,s,0,9600,MT175" name)
    fields:                     # paths relative to the sensor object
      power_w: P
      energy_in_kwh: E_in
      energy_out_kwh: E_out
      phase_power_w: [L1, L2, L3]
    # invert_sign: true         # optional: script reports export as positive

Field entries are the same as a device's ``fields`` (path or ``{path, scale}``,
``phase_*`` lists, ``extra``).  If the device names its sensor differently
(users often rename the meter in the script), the one sensor object holding
the power value is used.

Bundled profiles live next to this module in ``profiles/``; a device selects
one by file stem (``profile: iskra_mt175``) or points at its own file
(``profile: /config/my_meter.yaml``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from .mapping import (
    MappingError,
    PayloadError,
    build_field_map,
    resolve,
    sensors,
)

PROFILE_DIR = Path(__file__).parent / "profiles"
_KEYS = {"name", "description", "sensor", "fields", "invert_sign"}


class ProfileError(ValueError):
    """The profile file is missing or malformed."""


@dataclass(frozen=True)
class TasmotaProfile:
    key: str
    name: str
    sensor: str
    fields: dict[str, Any]
    invert_sign: bool = False
    description: str = ""


def available_profiles() -> list[str]:
    """Names of the bundled profiles."""
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))


def load_profile(ref: str | Path) -> TasmotaProfile:
    """Load a profile by bundled name or by file path."""
    ref_str = str(ref).strip()
    is_path = "/" in ref_str or ref_str.endswith((".yaml", ".yml"))
    path = Path(ref_str) if is_path else PROFILE_DIR / f"{ref_str}.yaml"
    return _load(path, ref_str)


@cache
def _load(path: Path, ref: str) -> TasmotaProfile:
    if not path.is_file():
        raise ProfileError(
            f"Tasmota profile {ref!r} not found ({path}). "
            f"Bundled profiles: {', '.join(available_profiles()) or 'none'}"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return parse_profile(raw, key=path.stem)
    except ProfileError as exc:
        raise ProfileError(f"{path}: {exc}") from exc


def parse_profile(raw: Any, key: str = "unnamed") -> TasmotaProfile:
    """Validate a parsed profile document.  Unknown keys and bad fields fail at start-up."""
    if not isinstance(raw, dict):
        raise ProfileError("profile must be a mapping")
    unknown = set(raw) - _KEYS
    if unknown:
        raise ProfileError(f"unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(_KEYS))})")
    sensor = str(raw.get("sensor") or "").strip()
    if not sensor:
        raise ProfileError("'sensor' is required (the sensor object below StatusSNS)")
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        raise ProfileError("'fields' must be a mapping")
    try:
        build_field_map(base=fields, sensor=sensor)
    except MappingError as exc:
        raise ProfileError(str(exc)) from exc
    return TasmotaProfile(
        key=key,
        name=str(raw.get("name") or key),
        sensor=sensor,
        fields=fields,
        invert_sign=bool(raw.get("invert_sign", False)),
        description=str(raw.get("description") or "").strip(),
    )


def matching_profiles(payload: Any) -> list[str]:
    """Bundled profiles whose every mapped value is in one sensor object of *payload*."""
    try:
        sns = sensors(payload)
    except PayloadError:
        return []
    objects = [o for o in sns.values() if isinstance(o, dict)]
    matches: list[str] = []
    for key in available_profiles():
        try:
            profile = load_profile(key)
        except ProfileError:
            continue
        specs = build_field_map(base=profile.fields, sensor=profile.sensor).specs()
        if any(all(resolve(o, s, summable=True) is not None for s in specs) for o in objects):
            matches.append(key)
    return matches

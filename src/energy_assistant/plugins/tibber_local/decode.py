"""Decode raw meter telegrams delivered by the Tibber Pulse Bridge.

The bridge passes the meter's telegram through unchanged.  Most German smart
meters speak **SML 1.04** (binary); some send **plaintext** OBIS lines
(IEC 62056-21 style, e.g. ``1-0:16.7.0*255(000592*W)``).  Both are reduced to
a :class:`MeterReading`.

Undecodable input (CRC error, truncated telegram, HTML error page, …) raises
:class:`TelegramError`.  Callers skip such samples and keep the last value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_SML_ESCAPE = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"

# OBIS short code -> (field name, divisor to the reported unit)
_POWER = "16.7.0"          # net active power, W (import +, export -)
_POWER_IMPORT = "1.7.0"    # some meters: positive-only import power, W
_POWER_EXPORT = "2.7.0"    # some meters: positive-only export power, W
_ENERGY_IMPORT = "1.8.0"   # import counter, Wh
_ENERGY_EXPORT = "2.8.0"   # export counter, Wh
_FREQUENCY = "14.7.0"      # Hz
_PHASES = {
    "l1": ("36.7.0", "32.7.0", "31.7.0"),  # power W, voltage V, current A
    "l2": ("56.7.0", "52.7.0", "51.7.0"),
    "l3": ("76.7.0", "72.7.0", "71.7.0"),
}

_OBIS_SHORT = re.compile(r"(?:\d+-\d+:)?(\d+\.\d+\.\d+)(?:\*\d+)?$")
_PLAIN_LINE = re.compile(
    r"^(?:\d+-\d+:)?(\d+\.\d+\.\d+)(?:\*\d+)?\(([-+]?\d+(?:\.\d+)?)(?:\*([A-Za-z]+))?\)"
)


class TelegramError(ValueError):
    """The payload is not a complete, valid meter telegram."""


@dataclass(frozen=True)
class MeterReading:
    """One decoded meter telegram, stamped at receive time (UTC)."""

    timestamp: datetime
    power_w: float
    """Net active power.  Positive = importing, negative = exporting."""
    import_kwh: float | None = None
    export_kwh: float | None = None
    frequency_hz: float | None = None
    phases: dict[str, float] = field(default_factory=dict)
    """Per-phase values, keys like ``l1_power_w``, ``l2_voltage_v``, ``l3_current_a``."""


def decode_telegram(payload: bytes, *, received_at: datetime | None = None) -> MeterReading:
    """Decode an SML or plaintext telegram; raise :class:`TelegramError` if unusable."""
    if not payload:
        raise TelegramError("empty payload")
    if _SML_ESCAPE in payload[:64]:
        values = _decode_sml(payload)
    else:
        values = _decode_plain(payload)
    return _to_reading(values, received_at or datetime.now(timezone.utc))


def _decode_sml(payload: bytes) -> dict[str, tuple[float, str]]:
    from smllib import SmlStreamReader
    from smllib.errors import SmlLibException

    reader = SmlStreamReader()
    reader.add(payload)
    try:
        frame = reader.get_frame()
        if frame is None:
            raise TelegramError("incomplete SML telegram")
        entries = frame.get_obis()
    except SmlLibException as exc:
        raise TelegramError(f"invalid SML telegram: {type(exc).__name__}") from exc
    except TelegramError:
        raise
    except Exception as exc:  # noqa: BLE001 — smllib can raise plain errors on garbage
        raise TelegramError(f"invalid SML telegram: {exc!r}") from exc

    values: dict[str, tuple[float, str]] = {}
    for entry in entries:
        match = _OBIS_SHORT.match(str(getattr(entry.obis, "obis_short", "")))
        if not match:
            continue
        try:
            value = entry.get_value()
        except Exception:  # noqa: BLE001 — text values (manufacturer, serial) etc.
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        # DLMS unit 30 = Wh; everything we use otherwise is already W/V/A/Hz.
        values[match.group(1)] = (float(value), "Wh" if entry.unit == 30 else "")
    return values


def _decode_plain(payload: bytes) -> dict[str, tuple[float, str]]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TelegramError("payload is neither SML nor ASCII text") from exc
    values: dict[str, tuple[float, str]] = {}
    for line in text.splitlines():
        match = _PLAIN_LINE.match(line.strip())
        if match:
            code, number, unit = match.groups()
            values[code] = (float(number), unit or "")
    return values


def _energy_kwh(values: dict[str, tuple[float, str]], code: str) -> float | None:
    if code not in values:
        return None
    value, unit = values[code]
    return value if unit.lower() == "kwh" else value / 1000.0


def _to_reading(values: dict[str, tuple[float, str]], ts: datetime) -> MeterReading:
    if _POWER in values:
        power = values[_POWER][0]
    elif _POWER_IMPORT in values or _POWER_EXPORT in values:
        power = values.get(_POWER_IMPORT, (0.0, ""))[0] - values.get(_POWER_EXPORT, (0.0, ""))[0]
    else:
        raise TelegramError("telegram contains no active power (OBIS 16.7.0 or 1.7.0/2.7.0)")

    phases: dict[str, float] = {}
    for phase, (p, v, i) in _PHASES.items():
        for code, name in ((p, "power_w"), (v, "voltage_v"), (i, "current_a")):
            if code in values:
                phases[f"{phase}_{name}"] = values[code][0]

    return MeterReading(
        timestamp=ts,
        power_w=power,
        import_kwh=_energy_kwh(values, _ENERGY_IMPORT),
        export_kwh=_energy_kwh(values, _ENERGY_EXPORT),
        frequency_hz=values[_FREQUENCY][0] if _FREQUENCY in values else None,
        phases=phases,
    )

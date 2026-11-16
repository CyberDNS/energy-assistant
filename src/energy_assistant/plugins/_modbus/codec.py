"""Register codec — converts between 16-bit Modbus words and Python numbers.

Pure functions with no I/O, so profiles and devices can be tested without a
Modbus connection.

A Modbus register is one unsigned 16-bit word.  Wider values span several
consecutive registers; ``word_order`` says whether the most significant word
comes first (``"big"``, the common case and what SMA uses) or last
(``"little"``).  Bytes inside a word are always big-endian, as the Modbus
specification requires.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, Literal

WordOrder = Literal["big", "little"]

# type name -> (struct format, number of 16-bit registers)
_TYPES: dict[str, tuple[str, int]] = {
    "s16": ("h", 1),
    "u16": ("H", 1),
    "s32": ("i", 2),
    "u32": ("I", 2),
    "s64": ("q", 4),
    "u64": ("Q", 4),
    "f32": ("f", 2),
    "f64": ("d", 4),
}

# Largest register count one read request may carry (Modbus specification).
MAX_READ_COUNT = 125


def is_known_type(type_name: str) -> bool:
    return type_name in _TYPES


def known_types() -> list[str]:
    return sorted(_TYPES)


def word_count(type_name: str) -> int:
    """Number of 16-bit registers a value of *type_name* occupies."""
    return _TYPES[type_name][1]


def _ordered(words: list[int], word_order: WordOrder) -> list[int]:
    return list(words) if word_order == "big" else list(reversed(words))


def raw_unsigned(words: list[int], word_order: WordOrder = "big") -> int:
    """Return *words* as one unsigned integer — used to match "no value" sentinels."""
    result = 0
    for word in _ordered(words, word_order):
        result = (result << 16) | (word & 0xFFFF)
    return result


def decode(words: list[int], type_name: str, word_order: WordOrder = "big") -> int | float:
    """Decode register *words* into a number of *type_name*."""
    fmt, count = _TYPES[type_name]
    if len(words) != count:
        raise ValueError(f"{type_name} needs {count} register(s), got {len(words)}")
    packed = struct.pack(f">{count}H", *(w & 0xFFFF for w in _ordered(words, word_order)))
    return struct.unpack(f">{fmt}", packed)[0]


def encode(value: int | float, type_name: str, word_order: WordOrder = "big") -> list[int]:
    """Encode *value* as register words of *type_name*.

    Raises ``ValueError`` when the value does not fit the type.
    """
    fmt, count = _TYPES[type_name]
    try:
        packed = struct.pack(f">{fmt}", value)
    except struct.error as exc:
        raise ValueError(f"{value!r} does not fit {type_name}: {exc}") from exc
    return _ordered(list(struct.unpack(f">{count}H", packed)), word_order)


@dataclass(frozen=True)
class ReadBlock:
    """One read request: *count* consecutive registers of *table* from *address*."""

    table: str
    address: int
    count: int

    def slice(self, address: int, count: int, words: list[int]) -> list[int]:
        """Cut the words of the register at *address* out of this block's *words*."""
        start = address - self.address
        return words[start : start + count]

    def covers(self, table: str, address: int, count: int) -> bool:
        return (
            table == self.table
            and address >= self.address
            and address + count <= self.address + self.count
        )


def plan_reads(spans: Iterable[tuple[str, int, int]]) -> list[ReadBlock]:
    """Group ``(table, address, count)`` spans into as few read requests as possible.

    Only directly adjacent (or overlapping) spans of the same table are merged.
    Gaps are never read across: many devices answer with an exception when a
    request touches an address they do not implement.
    """
    blocks: list[ReadBlock] = []
    for table, address, count in sorted(set(spans)):
        last = blocks[-1] if blocks else None
        if (
            last is not None
            and last.table == table
            and address <= last.address + last.count
            and max(last.address + last.count, address + count) - last.address <= MAX_READ_COUNT
        ):
            end = max(last.address + last.count, address + count)
            blocks[-1] = ReadBlock(table, last.address, end - last.address)
        else:
            blocks.append(ReadBlock(table, address, count))
    return blocks

"""In-memory Modbus client for tests — implements ``ModbusClientProtocol``."""

from __future__ import annotations

from energy_assistant.plugins._modbus.client import ModbusError


class FakeModbusClient:
    """Register store keyed by ``(table, address)``.

    Reading an address that was never set raises ``ModbusError``, like a real
    device answering "illegal data address".  ``reads`` and ``written`` record
    every request so tests can assert on batching and on what was written.
    """

    def __init__(self, registers: dict[tuple[str, int], int] | None = None) -> None:
        self.registers: dict[tuple[str, int], int] = dict(registers or {})
        self.reads: list[tuple[str, int, int, int]] = []
        self.written: list[tuple[int, list[int], int]] = []
        self.offline = False

    def set(self, table: str, address: int, words: list[int]) -> None:
        for i, word in enumerate(words):
            self.registers[(table, address + i)] = word

    async def read(self, table: str, address: int, count: int, unit_id: int) -> list[int]:
        self.reads.append((table, address, count, unit_id))
        if self.offline:
            raise ModbusError("offline")
        try:
            return [self.registers[(table, address + i)] for i in range(count)]
        except KeyError:
            raise ModbusError(f"illegal data address {table} {address}+{count}") from None

    async def write(self, address: int, values: list[int], unit_id: int) -> None:
        if self.offline:
            raise ModbusError("offline")
        self.written.append((address, list(values), unit_id))
        self.set("holding", address, list(values))

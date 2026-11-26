"""Modbus TCP client — thin async wrapper around ``pymodbus``.

The wrapper exists so that the rest of the platform depends on a tiny
protocol (``ModbusClientProtocol``) rather than on pymodbus itself:

* tests substitute an in-memory fake without opening sockets,
* requests are serialised — a Modbus TCP connection carries one transaction
  at a time, and several devices may share one connection (same gateway,
  different unit ids),
* the connection is opened lazily and re-opened after a failure, so a device
  that is offline at start-up simply reports ``available=False`` until it
  comes back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

_log = logging.getLogger(__name__)

TABLE_INPUT = "input"
TABLE_HOLDING = "holding"
TABLES = (TABLE_HOLDING, TABLE_INPUT)


class ModbusError(Exception):
    """A Modbus request failed (connection problem or exception response)."""


class ModbusClientProtocol(Protocol):
    """What a Modbus device plugin needs from the transport."""

    async def read(self, table: str, address: int, count: int, unit_id: int) -> list[int]:
        """Read *count* registers of *table* (``"input"`` / ``"holding"``)."""
        ...

    async def write(self, address: int, values: list[int], unit_id: int) -> None:
        """Write *values* to consecutive holding registers starting at *address*."""
        ...


class ModbusTcpClient:
    """One Modbus TCP connection to ``host:port``."""

    def __init__(self, host: str, port: int = 502, timeout: float = 3.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        # Created on first use: device factories run before the event loop
        # exists, and pymodbus binds its client to the running loop.
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        if self._client is None:
            from pymodbus.client import AsyncModbusTcpClient

            # reconnect_delay=0 turns off pymodbus' background reconnect loop;
            # reconnecting happens here instead, so there is exactly one place
            # that opens the connection.
            self._client = AsyncModbusTcpClient(
                self._host,
                port=self._port,
                timeout=self._timeout,
                retries=1,
                reconnect_delay=0,
            )
        if self._client.connected:
            return
        try:
            connected = await self._client.connect()
        except Exception as exc:  # noqa: BLE001
            raise ModbusError(f"cannot connect to {self._host}:{self._port}: {exc}") from exc
        if not connected:
            raise ModbusError(f"cannot connect to {self._host}:{self._port}")

    async def read(self, table: str, address: int, count: int, unit_id: int) -> list[int]:
        if table not in TABLES:
            raise ValueError(f"unknown register table {table!r}")
        async with self._lock:
            await self._ensure_connected()
            reader = (
                self._client.read_input_registers
                if table == TABLE_INPUT
                else self._client.read_holding_registers
            )
            try:
                response = await reader(address, count=count, device_id=unit_id)
            except Exception as exc:  # noqa: BLE001
                raise ModbusError(
                    f"read {table} {address}+{count} (unit {unit_id}) failed: {exc}"
                ) from exc
            if response.isError():
                raise ModbusError(
                    f"read {table} {address}+{count} (unit {unit_id}) rejected: {response}"
                )
            return list(response.registers)

    async def write(self, address: int, values: list[int], unit_id: int) -> None:
        async with self._lock:
            await self._ensure_connected()
            try:
                # One register goes out as "write single register" (function
                # code 6), several as "write multiple registers" (16) — the
                # same choice the ioBroker Modbus adapter makes by default.
                if len(values) == 1:
                    response = await self._client.write_register(
                        address, values[0], device_id=unit_id
                    )
                else:
                    response = await self._client.write_registers(
                        address, list(values), device_id=unit_id
                    )
            except Exception as exc:  # noqa: BLE001
                raise ModbusError(
                    f"write holding {address} (unit {unit_id}) failed: {exc}"
                ) from exc
            if response.isError():
                raise ModbusError(
                    f"write holding {address} (unit {unit_id}) rejected: {response}"
                )

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                self._client.close()


class ModbusConnectionPool:
    """One ``ModbusTcpClient`` per ``(host, port)`` pair.

    Devices behind the same gateway differ only in their unit id and must
    share a connection — many inverters accept only a few parallel ones.
    """

    def __init__(self) -> None:
        self._pool: dict[tuple[str, int], ModbusTcpClient] = {}
        self._used: set[tuple[str, int]] = set()

    def begin_build(self) -> None:
        """Start a rebuild of all devices; see ``sweep``."""
        self._used.clear()

    async def sweep(self) -> None:
        """Close the connections no device asked for since ``begin_build``."""
        for key in [k for k in self._pool if k not in self._used]:
            await self._pool.pop(key).close()

    def get(self, host: str, port: int = 502, timeout: float = 3.0) -> ModbusTcpClient:
        key = (host, port)
        self._used.add(key)
        if key not in self._pool:
            self._pool[key] = ModbusTcpClient(host, port, timeout)
        return self._pool[key]

    async def close_all(self) -> None:
        for client in self._pool.values():
            await client.close()
        self._pool.clear()

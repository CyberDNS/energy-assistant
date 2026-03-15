"""
ioBroker connection pool.

Creates and caches one ``IoBrokerClient`` per ``(host, port)`` pair so that
multiple plugins sharing the same ioBroker instance reuse a single HTTP
connection pool instead of each opening their own.

Usage
-----
Instantiate one ``IoBrokerConnectionPool`` at application startup and pass
it to every plugin factory that needs ioBroker access::

    pool = IoBrokerConnectionPool()

    tibber_tariff = TibberIoBrokerTariff(
        tariff_id="tibber",
        client=pool.get("192.168.2.30"),
        ...
    )
    fronius_device = IoBrokerDevice(
        device_id="solar",
        client=pool.get("192.168.2.30"),   # ← same client instance
        ...
    )

    # On shutdown:
    await pool.close_all()

Notes
-----
- The pool is intentionally not a global singleton.  Pass it explicitly so
  that test code can create isolated pools.
- If two callers request the same ``(host, port)`` with *different*
  ``api_token`` values, the first token wins and a warning is logged.
  In practice, home setups use one token for the whole ioBroker instance.
"""

from __future__ import annotations

import logging

from .client import IoBrokerClient

_log = logging.getLogger(__name__)


class IoBrokerConnectionPool:
    """
    Vends one shared ``IoBrokerClient`` per ``(host, port)`` pair.

    Thread-safety note: ``asyncio`` is single-threaded by design; no locking
    is required for coroutine-based code.
    """

    def __init__(self) -> None:
        self._clients: dict[tuple[str, int], IoBrokerClient] = {}
        self._tokens: dict[tuple[str, int], str | None] = {}

    def get(
        self,
        host: str,
        port: int = 8087,
        api_token: str | None = None,
        timeout: float = 10.0,
    ) -> IoBrokerClient:
        """
        Return the shared client for ``(host, port)``, creating it if needed.

        Parameters
        ----------
        host:
            ioBroker hostname or IP.
        port:
            simple-api adapter port (default 8087).
        api_token:
            Bearer token.  Ignored on subsequent calls for the same
            ``(host, port)`` — the first token wins.
        timeout:
            Request timeout in seconds.  Only used when creating a new client.
        """
        key = (host, port)
        if key not in self._clients:
            self._clients[key] = IoBrokerClient(host, port, api_token, timeout)
            self._tokens[key] = api_token
        elif self._tokens[key] != api_token:
            _log.warning(
                "IoBrokerConnectionPool: ignoring different api_token for %s:%d "
                "(first-registered token is used)",
                host,
                port,
            )
        return self._clients[key]

    async def close_all(self) -> None:
        """Close all managed clients and clear the pool."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        self._tokens.clear()

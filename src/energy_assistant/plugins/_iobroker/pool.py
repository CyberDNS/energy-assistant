"""Connection pool — one IoBrokerClient per (host, port) pair.

Pass a single pool instance to all ioBroker-based plugins so they share
one underlying HTTP connection to the same ioBroker instance.
"""

from __future__ import annotations

from .client import IoBrokerClient

_PoolKey = tuple[str, int]


class IoBrokerConnectionPool:
    """One ``IoBrokerClient`` per ``(host, port)`` pair.

    Usage
    -----
    ::

        pool = IoBrokerConnectionPool()
        client = pool.get("192.168.1.5")          # port defaults to 8087
        client2 = pool.get("192.168.1.5", 8087)   # same object returned
    """

    def __init__(self) -> None:
        self._pool: dict[_PoolKey, IoBrokerClient] = {}

    def get(
        self,
        host: str,
        port: int = 8087,
        api_token: str | None = None,
        timeout: float = 5.0,
    ) -> IoBrokerClient:
        """Return (or create) the client for *(host, port)*."""
        key: _PoolKey = (host, port)
        if key not in self._pool:
            self._pool[key] = IoBrokerClient(
                host=host,
                port=port,
                api_token=api_token,
                timeout=timeout,
            )
        return self._pool[key]

    async def close_all(self) -> None:
        """Close all pooled connections."""
        for client in self._pool.values():
            await client.close()
        self._pool.clear()

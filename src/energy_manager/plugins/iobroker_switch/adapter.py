"""
ioBroker boolean switch control adapter.

Turns an ioBroker datapoint on/off by writing a configurable value
(default ``True`` / ``False``).
"""

from __future__ import annotations

from typing import Any


class IoBrokerSwitchAdapter:
    """
    Controls a writable boolean OID in ioBroker.

    Parameters
    ----------
    client:
        Open ``IoBrokerClient`` or compatible fake — must implement
        ``set_value(oid, value)``.
    oid:
        The OID of the boolean datapoint.
    on_value:
        Value written when turning on (default ``True``).
    off_value:
        Value written when turning off (default ``False``).
    """

    def __init__(
        self,
        client: Any,
        oid: str,
        *,
        on_value: Any = True,
        off_value: Any = False,
    ) -> None:
        self._client = client
        self._oid = oid
        self._on_value = on_value
        self._off_value = off_value

    async def turn_on(self) -> None:
        await self._client.set_value(self._oid, self._on_value)

    async def turn_off(self) -> None:
        await self._client.set_value(self._oid, self._off_value)

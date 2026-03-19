"""IoBrokerSwitchAdapter — writes a boolean OID in ioBroker.

Implements the ``Switchable`` protocol structurally.
"""

from __future__ import annotations

from typing import Any

from .._iobroker.client import IoBrokerClientProtocol


class IoBrokerSwitchAdapter:
    """Controls a device by writing a boolean (or custom) OID in ioBroker.

    Parameters
    ----------
    client:
        An open ioBroker client.
    oid:
        The OID to write when turning on or off.
    on_value:
        Value written on ``turn_on()``.  Defaults to ``True``.
    off_value:
        Value written on ``turn_off()``.  Defaults to ``False``.
    """

    def __init__(
        self,
        client: IoBrokerClientProtocol,
        oid: str,
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

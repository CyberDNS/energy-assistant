"""HASwitchAdapter — calls homeassistant.turn_on/off via the HA REST API.

Implements the ``Switchable`` protocol structurally.
"""

from __future__ import annotations

from .._homeassistant.client import HAClientProtocol


class HASwitchAdapter:
    """Controls a device by calling the ``homeassistant.turn_on/off`` service.

    Parameters
    ----------
    client:
        An open Home Assistant client.
    entity_id:
        The HA entity to switch, e.g. ``"switch.heatpump_main"``.
    """

    def __init__(self, client: HAClientProtocol, entity_id: str) -> None:
        self._client = client
        self._entity_id = entity_id

    async def turn_on(self) -> None:
        await self._client.call_service(
            "homeassistant", "turn_on", {"entity_id": self._entity_id}
        )

    async def turn_off(self) -> None:
        await self._client.call_service(
            "homeassistant", "turn_off", {"entity_id": self._entity_id}
        )

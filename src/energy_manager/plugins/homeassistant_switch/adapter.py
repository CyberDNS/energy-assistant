"""
Home Assistant switch control adapter.

Turns a Home Assistant switch entity on/off by calling the
``homeassistant.turn_on`` / ``homeassistant.turn_off`` services via the
HA REST API.
"""

from __future__ import annotations

from typing import Any


class HASwitchAdapter:
    """
    Controls a Home Assistant switch entity.

    Parameters
    ----------
    client:
        Open ``HAClient`` or compatible fake — must implement
        ``call_service(domain, service, data)``.
    entity_id:
        The full HA entity ID, e.g. ``"switch.heatpump_main"``.
    """

    def __init__(self, client: Any, entity_id: str) -> None:
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

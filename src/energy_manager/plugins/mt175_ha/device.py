"""
MT175 smart meter grid power reading backed directly by Home Assistant.

Reads the instantaneous net grid power from the MT175 smart meter exposed
as a Home Assistant sensor.  No ioBroker required.

Default entity ID::

    sensor.mt175_mt175_p

The state value is net grid power in Watts:

- Positive  = importing from grid (consuming)
- Negative  = exporting to grid   (feeding in)

Usage::

    async with HAClient(host, token=token) as client:
        device = MT175HADevice(device_id="mt175", client=client)
        state = await device.get_state()
        # state.power_w  — net grid W (positive=import, negative=export)
"""

from __future__ import annotations

from datetime import datetime, timezone

from .._homeassistant.client import HAClientProtocol
from ...core.models import DeviceCategory, DeviceState

_DEFAULT_ENTITY_ID = "sensor.mt175_mt175_p"


class MT175HADevice:
    """
    Reads instantaneous net grid power from the MT175 smart meter via
    the Home Assistant REST API.

    This device is **read-only** and categorised as a METER.

    Parameters
    ----------
    device_id:
        Stable identifier used throughout the platform (e.g. ``"mt175"``).
    client:
        An open ``HAClient`` (or any ``HAClientProtocol`` implementation).
    entity_id:
        Override the Home Assistant entity ID.
        Defaults to ``sensor.mt175_mt175_p``.
    """

    def __init__(
        self,
        device_id: str,
        client: HAClientProtocol,
        *,
        entity_id: str | None = None,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._entity_id = entity_id or _DEFAULT_ENTITY_ID

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.METER

    async def get_state(self) -> DeviceState:
        """
        Read the current net grid power measurement.

        Returns a ``DeviceState`` where:

        - ``power_w``: net grid power in W
          (positive = importing, negative = exporting)
        """
        raw = await self._client.get_entity_state(self._entity_id)
        try:
            power_w: float | None = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            power_w = None

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=power_w,
        )

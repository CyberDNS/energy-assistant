"""
Tibber live power reading backed by the ioBroker *tibberlink* adapter.

The tibberlink adapter pushes real-time measurements from Tibber's Pulse
device into ioBroker under::

    tibberlink.0.Homes.<HOME_ID>.LiveMeasurement.<field>

The field used here is:

- ``power``  — instantaneous net power (W).
  Positive = consuming from grid, negative = feeding into grid.

Usage::

    device = TibberLivePowerIoBrokerDevice(
        device_id="tibber_live",
        client=client,
        home_id="aa115263-6d29-4e80-8190-fb95ddd4e743",
    )
    state = await device.get_state()
    # state.power_w  — net grid W (positive=import, negative=export)
"""

from __future__ import annotations

from datetime import datetime, timezone

from .._iobroker.client import IoBrokerClientProtocol
from ...core.models import DeviceCategory, DeviceState

_ADAPTER_PREFIX = "tibberlink.0.Homes"
_LIVE_FIELD = "LiveMeasurement.power"


class TibberLivePowerIoBrokerDevice:
    """
    Reads instantaneous grid power from the Tibber Pulse via ioBroker.

    This device is **read-only** and categorised as a METER.

    Parameters
    ----------
    device_id:
        Stable identifier used throughout the platform (e.g. ``"tibber_live"``).
    client:
        An open ``IoBrokerClient``.
    home_id:
        Tibber home UUID as shown in the ioBroker tibberlink object tree.
    oid_power_w:
        Override the ioBroker OID for live power.
        Defaults to ``tibberlink.0.Homes.<home_id>.LiveMeasurement.power``.
    """

    def __init__(
        self,
        device_id: str,
        client: IoBrokerClientProtocol,
        *,
        home_id: str,
        oid_power_w: str | None = None,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._oid_power_w = (
            oid_power_w or f"{_ADAPTER_PREFIX}.{home_id}.{_LIVE_FIELD}"
        )

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.METER

    async def get_state(self) -> DeviceState:
        """
        Read the current live power measurement.

        Returns a ``DeviceState`` where:

        - ``power_w``: net grid power in W
          (positive = importing, negative = exporting)
        """
        raw = await self._client.get_bulk([self._oid_power_w])
        val = raw.get(self._oid_power_w)
        try:
            power_w: float | None = float(val) if val is not None else None
        except (TypeError, ValueError):
            power_w = None

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=power_w,
        )

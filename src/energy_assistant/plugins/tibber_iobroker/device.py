"""TibberIoBrokerDevice — power meter backed by the ioBroker tibberlink adapter.

Reads live power from ``LiveMeasurement.power``, which is the real-time
consumption reported by the Tibber Pulse/Bridge clipped to the meter.

OIDs used
---------
``tibberlink.0.Homes.<HOME_ID>.LiveMeasurement.power``
    Net power in watts at the metering point.
    Positive = importing from grid, negative = exporting (PV surplus).

Sign convention matches the rest of the platform:
``power_w > 0``  — consuming / importing
``power_w < 0``  — producing / exporting
"""

from __future__ import annotations

import logging

from ...core.models import DeviceCommand, DeviceRole, DeviceState
from .._iobroker.client import IoBrokerClientProtocol

_log = logging.getLogger(__name__)

_POWER_OID = "tibberlink.0.Homes.{home_id}.LiveMeasurement.power"


class TibberIoBrokerDevice:
    """A read-only ``Device`` that reads live power from ioBroker tibberlink.

    Implements the ``Device`` protocol structurally (no inheritance).

    Parameters
    ----------
    device_id:
        Stable, unique identifier for this device.
    role:
        Semantic role (typically ``DeviceRole.METER``).
    client:
        An open ioBroker client (e.g. from ``IoBrokerConnectionPool``).
    home_id:
        Tibber home ID — used to derive the ioBroker OID automatically.
    """

    def __init__(
        self,
        device_id: str,
        role: DeviceRole,
        client: IoBrokerClientProtocol,
        home_id: str,
    ) -> None:
        self._device_id = device_id
        self._role = role
        self._client = client
        self._home_id = home_id
        self._oid = _POWER_OID.format(home_id=home_id)

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

    @property
    def home_id(self) -> str:
        return self._home_id

    async def get_state(self) -> DeviceState:
        """Read live power from ioBroker tibberlink and return a ``DeviceState``."""
        try:
            raw = await self._client.get_value(self._oid)
            power_w = float(raw) if raw is not None else None
            return DeviceState(
                device_id=self._device_id,
                power_w=power_w,
                available=power_w is not None,
            )
        except Exception:
            _log.warning("TibberIoBrokerDevice %r: failed to read %r", self._device_id, self._oid)
            return DeviceState(device_id=self._device_id, power_w=None, available=False)

    async def send_command(self, command: DeviceCommand) -> None:
        """No-op — tibberlink is read-only."""

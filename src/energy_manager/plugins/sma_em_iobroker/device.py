"""
SMA Energy Manager grid meter backed by the ioBroker *sma-em* adapter.

The ioBroker SMA EM adapter publishes per-device OIDs under::

    sma-em.0.<serial>.<channel>

The two channels used here are:

- ``pregard``  — Power currently drawn from the grid (W); always >= 0.
- ``psurplus`` — Power currently fed into the grid (W); always >= 0.

``power_w`` on the returned ``DeviceState`` is the **net** grid exchange:
positive = net import (drawing from grid), negative = net export (feeding grid).

Usage::

    device = SMAEMIoBrokerDevice(
        device_id="grid_meter",
        client=client,
        serial="3008815327",
    )
    state = await device.get_state()
    # state.power_w   — net grid W (+import / -export)
    # state.extra["import_w"]  — pregard (W)
    # state.extra["export_w"]  — psurplus (W)
"""

from __future__ import annotations

from datetime import datetime, timezone

from .._iobroker.client import IoBrokerClientProtocol
from ...core.models import DeviceCategory, DeviceState

_ADAPTER_PREFIX = "sma-em.0"


class SMAEMIoBrokerDevice:
    """
    Reads SMA Energy Manager grid power from ioBroker's sma-em adapter.

    This device is **read-only** and categorised as a METER.  It is not
    controllable and does not expose ``storage_constraints``.

    Parameters
    ----------
    device_id:
        Stable identifier used throughout the platform (e.g. ``"grid_meter"``).
    client:
        An open ``IoBrokerClient``.
    serial:
        The serial number of the SMA Energy Manager as shown in the ioBroker
        sma-em object tree (e.g. ``"3008815327"``).
    oid_import_w:
        Override the ioBroker OID for grid import power (pregard).
        Defaults to ``sma-em.0.<serial>.pregard``.
    oid_export_w:
        Override the ioBroker OID for grid export power (psurplus).
        Defaults to ``sma-em.0.<serial>.psurplus``.
    """

    def __init__(
        self,
        device_id: str,
        client: IoBrokerClientProtocol,
        *,
        serial: str,
        oid_import_w: str | None = None,
        oid_export_w: str | None = None,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._oid_import_w = oid_import_w or f"{_ADAPTER_PREFIX}.{serial}.pregard"
        self._oid_export_w = oid_export_w or f"{_ADAPTER_PREFIX}.{serial}.psurplus"

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.METER

    async def get_state(self) -> DeviceState:
        """
        Read grid import and export power in one bulk request.

        Returns a ``DeviceState`` where:

        - ``power_w``: net grid exchange (positive = import, negative = export)
        - ``extra["import_w"]``: raw pregard value (W, always >= 0)
        - ``extra["export_w"]``: raw psurplus value (W, always >= 0)
        """
        raw = await self._client.get_bulk([self._oid_import_w, self._oid_export_w])

        def _float(oid: str) -> float | None:
            val = raw.get(oid)
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        import_w = _float(self._oid_import_w)
        export_w = _float(self._oid_export_w)

        if import_w is not None and export_w is not None:
            net_w: float | None = import_w - export_w
        elif import_w is not None:
            net_w = import_w
        elif export_w is not None:
            net_w = -export_w
        else:
            net_w = None

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=net_w,
            extra={
                "import_w": import_w,
                "export_w": export_w,
            },
        )

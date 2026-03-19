"""GenericIoBrokerDevice — reads a power sensor from ioBroker simple-api.

Implements the ``Device`` protocol directly; no base class required.
Read-only: ``send_command`` is a no-op.  For controllable devices, pair
with an ``IoBrokerSwitchAdapter`` and an ``OverflowStrategy``.

OID modes
---------
``oid_power``
    Single OID returning the net power value.
    ``power_w = raw_value``

``oid_power_import`` + ``oid_power_export``
    Two OIDs for bidirectional meters (e.g. the main grid connection in
    Messkonzept 8 where import and export are metered separately).
    ``power_w = import_w − export_w``
    Both ``import_w`` and ``export_w`` are also stored in ``DeviceState.extra``.

Sign convention
---------------
``power_w > 0``  — consuming from grid / importing
``power_w < 0``  — exporting to grid / producing
"""

from __future__ import annotations

import logging

from ...core.models import DeviceCommand, DeviceRole, DeviceState
from .._iobroker.client import IoBrokerClientProtocol

_log = logging.getLogger(__name__)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GenericIoBrokerDevice:
    """A ``Device`` that reads power from the ioBroker simple-api.

    Parameters
    ----------
    device_id:
        Stable, unique identifier for this device.
    role:
        Semantic role (``METER``, ``CONSUMER``, ``PRODUCER``, …).
    client:
        An open ioBroker client (e.g. from ``IoBrokerConnectionPool``).
    oid_power:
        OID for the net power value (W).  Mutually exclusive with the
        import/export pair.
    oid_power_import:
        OID for gross import power (W).  Requires ``oid_power_export``.
    oid_power_export:
        OID for gross export power (W).  Requires ``oid_power_import``.
    """

    def __init__(
        self,
        device_id: str,
        role: DeviceRole,
        client: IoBrokerClientProtocol,
        *,
        oid_power: str | None = None,
        oid_power_import: str | None = None,
        oid_power_export: str | None = None,
    ) -> None:
        has_single = oid_power is not None
        has_pair = oid_power_import is not None and oid_power_export is not None
        if not has_single and not has_pair:
            raise ValueError(
                f"GenericIoBrokerDevice '{device_id}': provide 'oid_power' or "
                "both 'oid_power_import' and 'oid_power_export'."
            )
        self._device_id = device_id
        self._role = role
        self._client = client
        self._oid_power = oid_power
        self._oid_power_import = oid_power_import
        self._oid_power_export = oid_power_export

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

    async def get_state(self) -> DeviceState:
        """Read power from ioBroker and return a ``DeviceState``."""
        try:
            power_w: float | None = None
            extra: dict = {}

            if self._oid_power:
                raw = await self._client.get_value(self._oid_power)
                power_w = _to_float(raw)

            else:
                assert self._oid_power_import and self._oid_power_export
                values = await self._client.get_bulk(
                    [self._oid_power_import, self._oid_power_export]
                )
                import_w = _to_float(values.get(self._oid_power_import))
                export_w = _to_float(values.get(self._oid_power_export))
                if import_w is not None and export_w is not None:
                    power_w = import_w - export_w
                    extra["import_w"] = import_w
                    extra["export_w"] = export_w

            return DeviceState(
                device_id=self._device_id,
                power_w=power_w,
                available=power_w is not None,
                extra=extra,
            )

        except Exception:
            _log.warning(
                "Failed to read device %r from ioBroker", self._device_id, exc_info=True
            )
            return DeviceState(device_id=self._device_id, available=False)

    async def send_command(self, command: DeviceCommand) -> None:
        # Read-only device — commands are silently ignored.
        pass

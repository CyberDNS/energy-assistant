"""GenericHADevice — reads a power sensor from the Home Assistant REST API.

Implements the ``Device`` protocol directly; no base class required.
Read-only: ``send_command`` is a no-op.  For controllable devices, pair
with an ``HASwitchAdapter`` and an ``OverflowStrategy``.

Entity modes
------------
``entity_power``
    Single entity returning the net power value.
    ``power_w = float(state)``

``entity_power_import`` + ``entity_power_export``
    Two entities for bidirectional meters.
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
from .._homeassistant.client import HAClientProtocol

_log = logging.getLogger(__name__)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GenericHADevice:
    """A ``Device`` that reads power from the Home Assistant REST API.

    Parameters
    ----------
    device_id:
        Stable, unique identifier for this device.
    role:
        Semantic role (``METER``, ``CONSUMER``, ``PRODUCER``, …).
    client:
        An open HA client.
    entity_power:
        HA entity ID for the net power value (W).
    entity_power_import:
        HA entity ID for gross import power (W).  Requires ``entity_power_export``.
    entity_power_export:
        HA entity ID for gross export power (W).  Requires ``entity_power_import``.
    """

    def __init__(
        self,
        device_id: str,
        role: DeviceRole,
        client: HAClientProtocol,
        *,
        entity_power: str | None = None,
        entity_power_import: str | None = None,
        entity_power_export: str | None = None,
    ) -> None:
        has_single = entity_power is not None
        has_pair = entity_power_import is not None and entity_power_export is not None
        if not has_single and not has_pair:
            raise ValueError(
                f"GenericHADevice '{device_id}': provide 'entity_power' or "
                "both 'entity_power_import' and 'entity_power_export'."
            )
        self._device_id = device_id
        self._role = role
        self._client = client
        self._entity_power = entity_power
        self._entity_power_import = entity_power_import
        self._entity_power_export = entity_power_export

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

    async def get_state(self) -> DeviceState:
        """Read power from Home Assistant and return a ``DeviceState``."""
        try:
            power_w: float | None = None
            extra: dict = {}

            if self._entity_power:
                raw = await self._client.get_entity_state(self._entity_power)
                power_w = _to_float(raw)

            else:
                assert self._entity_power_import and self._entity_power_export
                import_raw = await self._client.get_entity_state(self._entity_power_import)
                export_raw = await self._client.get_entity_state(self._entity_power_export)
                import_w = _to_float(import_raw)
                export_w = _to_float(export_raw)
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
                "Failed to read device %r from Home Assistant",
                self._device_id,
                exc_info=True,
            )
            return DeviceState(device_id=self._device_id, available=False)

    async def send_command(self, command: DeviceCommand) -> None:
        # Read-only device — commands are silently ignored.
        pass

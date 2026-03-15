"""
Generic ioBroker Device implementation.

Maps ioBroker state object IDs to the normalised ``DeviceState`` model and
translates ``DeviceCommand`` instances back to ioBroker set operations.

This class is used by integration plugins (``tibber_iobroker``,
``fronius_iobroker``, etc.) directly or as a base building block.  It is
intentionally generic — it does not know about any specific adapter's object
naming scheme.

Known ``state_map`` keys (mapped to ``DeviceState`` fields)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- ``power_w``       — instantaneous power in watts
- ``energy_kwh``    — cumulative energy counter in kWh
- ``soc_pct``       — state of charge in percent (STORAGE devices)
- ``available``     — boolean availability flag

Any other key is stored verbatim in ``DeviceState.extra``.
"""

from __future__ import annotations

from typing import Any

from ...core.models import DeviceCategory, DeviceCommand, DeviceState
from .client import IoBrokerClientProtocol

# DeviceState field names that receive special handling (not dumped into extra).
_KNOWN_FIELDS: frozenset[str] = frozenset({"power_w", "energy_kwh", "soc_pct", "available"})


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.lower() not in {"false", "0", "off", "no"}
    return default


class IoBrokerDevice:
    """
    A ``Device`` backed by ioBroker state objects.

    Implements the ``Device`` protocol structurally — no subclassing required.

    Parameters
    ----------
    device_id:
        Stable unique identifier (must match the ``id`` in ``config.yaml``).
    category:
        Energy role of the device.
    client:
        An open ``IoBrokerClientProtocol`` instance (managed by the caller).
    state_map:
        Mapping of DeviceState field name → ioBroker object ID.
    command_map:
        Mapping of command name → ioBroker object ID.
        If ``None`` or empty the device is considered read-only.
    """

    def __init__(
        self,
        device_id: str,
        category: DeviceCategory,
        client: IoBrokerClientProtocol,
        state_map: dict[str, str],
        command_map: dict[str, str] | None = None,
    ) -> None:
        self._id = device_id
        self._category = category
        self._client = client
        self._state_map = state_map
        self._command_map: dict[str, str] = command_map or {}

    # ------------------------------------------------------------------
    # Device protocol
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._id

    @property
    def category(self) -> DeviceCategory:
        return self._category

    async def get_state(self) -> DeviceState:
        """
        Fetch all mapped object IDs concurrently and return a ``DeviceState``.

        Object IDs that ioBroker does not know return ``None``.
        Values for unknown fields (not in ``_KNOWN_FIELDS``) are placed in
        ``DeviceState.extra``.

        The device category is always stored in ``extra["category"]`` so the
        optimizer can identify the device role without needing additional context.
        """
        if not self._state_map:
            return DeviceState(
                device_id=self._id,
                extra={"category": self._category.value},
            )

        object_ids = list(self._state_map.values())
        raw_values = await self._client.get_bulk(object_ids)

        # Map field names to their fetched values.
        field_values: dict[str, Any] = {
            field: raw_values.get(oid)
            for field, oid in self._state_map.items()
        }

        extra: dict[str, Any] = {
            k: v for k, v in field_values.items() if k not in _KNOWN_FIELDS
        }
        # Always include category so the optimizer can categorise this device.
        extra["category"] = self._category.value

        return DeviceState(
            device_id=self._id,
            power_w=_to_float(field_values.get("power_w")),
            energy_kwh=_to_float(field_values.get("energy_kwh")),
            soc_pct=_to_float(field_values.get("soc_pct")),
            available=_to_bool(field_values.get("available")),
            extra=extra,
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """
        Write a control command to ioBroker.

        Raises ``NotImplementedError`` if the command is not in ``command_map``.
        """
        object_id = self._command_map.get(command.command)
        if object_id is None:
            available = list(self._command_map.keys())
            raise NotImplementedError(
                f"Command '{command.command}' is not mapped for device '{self._id}'. "
                f"Available commands: {available if available else '(none — device is read-only)'}"
            )
        await self._client.set_value(object_id, command.value)

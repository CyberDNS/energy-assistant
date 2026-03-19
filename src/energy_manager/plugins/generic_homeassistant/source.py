"""
Generic Home Assistant integration source.

Reads a power entity directly from the Home Assistant REST API and exposes
it as an ``IntegrationState``.  Configured via the ``generic_homeassistant``
source type in ``config.yaml``::

    integrations:
      - grid_meter:
          source:
            type: generic_homeassistant
            power: "sensor.mt175_mt175_p"
"""

from __future__ import annotations

from .._homeassistant.client import HAClientProtocol
from ...core.integration import IntegrationState


class GenericHASource:
    """
    Reads instantaneous power from Home Assistant entities.

    Either ``entity_power`` (net W) **or** both ``entity_power_import`` and
    ``entity_power_export`` must be provided.  When only import/export are
    given, ``power_w`` is derived as ``import - export``.

    Parameters
    ----------
    name:
        Integration name (used as the key in the registry).
    client:
        Open ``HAClient`` or compatible stub.
    entity_power:
        Home Assistant entity ID for net power (W).
    entity_power_import:
        Optional entity ID for import-only power (W).
    entity_power_export:
        Optional entity ID for export-only power (W).
    """

    def __init__(
        self,
        name: str,
        client: HAClientProtocol,
        *,
        entity_power: str | None = None,
        entity_power_import: str | None = None,
        entity_power_export: str | None = None,
    ) -> None:
        if entity_power is None and (entity_power_import is None or entity_power_export is None):
            raise ValueError(
                f"Integration {name!r}: provide either 'power' or both "
                "'power_import' and 'power_export'."
            )
        self.name = name
        self._client = client
        self._entity_power = entity_power
        self._entity_power_import = entity_power_import
        self._entity_power_export = entity_power_export

    async def read(self) -> IntegrationState:
        entities = [
            e for e in [
                self._entity_power,
                self._entity_power_import,
                self._entity_power_export,
            ]
            if e is not None
        ]
        raw: dict[str, object] = {}
        for entity_id in entities:
            raw[entity_id] = await self._client.get_entity_state(entity_id)

        def _float(entity_id: str | None) -> float | None:
            if entity_id is None:
                return None
            val = raw.get(entity_id)
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        power_import = _float(self._entity_power_import)
        power_export = _float(self._entity_power_export)

        power_w = _float(self._entity_power)
        if power_w is None and power_import is not None and power_export is not None:
            power_w = power_import - power_export

        return IntegrationState(name=self.name, power_w=power_w)

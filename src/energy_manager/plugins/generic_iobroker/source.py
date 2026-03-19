"""
Generic ioBroker integration source.

Reads one or more power OIDs from ioBroker and exposes them as an
``IntegrationState``.  Configured via the ``generic_iobroker`` source type
in the ``integrations:`` block of ``config.yaml``::

    integrations:
      - tibber:
          source:
            type: generic_iobroker
            power: "tibberlink.0.Homes.<id>.LiveMeasurement.power"
            # power_import: "tibberlink.0.Homes.<id>.LiveMeasurement.powerImport"
            # power_export: "tibberlink.0.Homes.<id>.LiveMeasurement.powerExport"
"""

from __future__ import annotations

from .._iobroker.client import IoBrokerClientProtocol
from ...core.integration import IntegrationState


class GenericIoBrokerSource:
    """
    Reads power measurement(s) from ioBroker OIDs.

    Parameters
    ----------
    name:
        Integration name (used as the key in the registry).
    client:
        Open ``IoBrokerClient`` or compatible stub.
    oid_power:
        OID for net / instantaneous power (W).
    oid_power_import:
        Optional OID for import-only power (W).
    oid_power_export:
        Optional OID for export-only power (W).
    """

    def __init__(
        self,
        name: str,
        client: IoBrokerClientProtocol,
        *,
        oid_power: str | None = None,
        oid_power_import: str | None = None,
        oid_power_export: str | None = None,
    ) -> None:
        if oid_power is None and (oid_power_import is None or oid_power_export is None):
            raise ValueError(
                f"Integration {name!r}: provide either 'power' or both 'power_import' "
                "and 'power_export'."
            )
        self.name = name
        self._client = client
        self._oid_power = oid_power
        self._oid_power_import = oid_power_import
        self._oid_power_export = oid_power_export

    async def read(self) -> IntegrationState:
        oids = [
            oid
            for oid in [self._oid_power, self._oid_power_import, self._oid_power_export]
            if oid is not None
        ]
        raw = await self._client.get_bulk(oids)

        def _float(oid: str | None) -> float | None:
            if oid is None:
                return None
            val = raw.get(oid)
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        power_import = _float(self._oid_power_import)
        power_export = _float(self._oid_power_export)

        # Derive net power from import/export when no direct power OID is set.
        power_w = _float(self._oid_power)
        if power_w is None and power_import is not None and power_export is not None:
            power_w = power_import - power_export

        return IntegrationState(name=self.name, power_w=power_w)

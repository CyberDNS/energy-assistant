"""
Home power monitor backed by Home Assistant entities in ioBroker.

Reads real-time home energy flows — total household consumption, PV generation,
PV overflow, and EV charger consumption — from Home Assistant sensor entities
exposed via the ioBroker *hass* adapter.

These readings are consumed by the controller's high-frequency (15-second)
control loop to make real-time charge/discharge decisions that complement the
hourly MILP plan.

Default OID paths follow the ioBroker *hass* adapter naming convention::

    hass.0.entities.sensor.<entity_id>.state

Override any of them in the ``home_power_iobroker:`` config section when
your Home Assistant entity IDs differ from the defaults.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .._iobroker.client import IoBrokerClientProtocol
from ...core.models import DeviceCategory, DeviceState

_DEFAULT_OID_HOUSEHOLD_W = "hass.0.entities.sensor.power_consumption_household.state"
_DEFAULT_OID_OVERFLOW_W = (
    "hass.0.entities.sensor.power_production_overflow_pv_only_household.state"
)
_DEFAULT_OID_CARS_W = "hass.0.entities.sensor.power_consumption_cars.state"
_DEFAULT_OID_PV_W = "hass.0.entities.sensor.power_production_pv.state"


class HomePowerIoBrokerDevice:
    """
    Reads real-time home energy flows from Home Assistant via ioBroker.

    Parameters
    ----------
    device_id:
        Stable identifier used throughout the platform (e.g. ``"home_power"``).
    client:
        An open ``IoBrokerClient``.
    oid_household_w:
        Total AC household consumption sensor OID.
    oid_overflow_w:
        PV surplus above household consumption (excess available for
        battery charging or grid export).
    oid_cars_w:
        EV charger power consumption sensor OID.
    oid_pv_w:
        PV generation sensor OID.
    """

    def __init__(
        self,
        device_id: str,
        client: IoBrokerClientProtocol,
        *,
        oid_household_w: str = _DEFAULT_OID_HOUSEHOLD_W,
        oid_overflow_w: str = _DEFAULT_OID_OVERFLOW_W,
        oid_cars_w: str = _DEFAULT_OID_CARS_W,
        oid_pv_w: str = _DEFAULT_OID_PV_W,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._oid_household_w = oid_household_w
        self._oid_overflow_w = oid_overflow_w
        self._oid_cars_w = oid_cars_w
        self._oid_pv_w = oid_pv_w

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.METER

    async def get_state(self) -> DeviceState:
        """
        Read all home power sensors in one bulk request.

        Returns a ``DeviceState`` where:

        - ``power_w``: total household AC consumption (W)
        - ``extra["overflow_w"]``: PV surplus available for battery / export (W)
        - ``extra["cars_w"]``: EV charger consumption (W)
        - ``extra["pv_w"]``: PV generation (W)
        """
        oids = [
            self._oid_household_w,
            self._oid_overflow_w,
            self._oid_cars_w,
            self._oid_pv_w,
        ]
        raw = await self._client.get_bulk(oids)

        def _float(oid: str) -> float | None:
            val = raw.get(oid)
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=_float(self._oid_household_w),
            extra={
                "overflow_w": _float(self._oid_overflow_w),
                "cars_w": _float(self._oid_cars_w),
                "pv_w": _float(self._oid_pv_w),
            },
        )

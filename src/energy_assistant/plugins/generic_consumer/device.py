"""GenericConsumerDevice — a virtual consumer with no hardware attachment.

Used to model any load that has a known consumption profile and tariff but
no directly-readable meter OID.  The prototypical example is a "baseline"
background load (always-on household devices such as NAS, router, fridge)
that cannot be individually measured but whose average consumption is known
from bills or calibration.

Because this device carries no real hardware the ``get_state()`` method
returns a synthesised DeviceState with ``power_w = 0.0`` and
``available = True``.  Its contribution to the energy plan comes entirely
from the static_profile (or any other) forecast declared in the config via
the ``forecast:`` sub-section; the planning loop reads that via
``build_device_forecasts()``.

Configuration example::

    - id: baseline
      role: consumer
      type: generic_consumer
      tariff: household       # tariff that bills this load (used for cost calc)
      forecast:
        type: static_profile
        profile:
          weekdays:
            - hour: 0
              consumed_kwh: 16.8   # 0.7 kW × 24 h
          weekends:
            - hour: 0
              consumed_kwh: 16.8
"""

from __future__ import annotations

from ...core.models import DeviceCommand, DeviceRole, DeviceState


class GenericConsumerDevice:
    """A virtual consumer device — declares load profile and tariff, no OIDs.

    Implements the ``Device`` protocol structurally.
    """

    def __init__(self, device_id: str, tariff_id: str | None = None) -> None:
        self._device_id = device_id
        self._tariff_id = tariff_id

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return DeviceRole.CONSUMER

    @property
    def tariff_id(self) -> str | None:
        """The tariff that bills this consumer's load (informational)."""
        return self._tariff_id

    async def get_state(self) -> DeviceState:
        """Returns a zero-power state — all load comes from the forecast."""
        return DeviceState(device_id=self._device_id, power_w=0.0, available=True)

    async def send_command(self, command: DeviceCommand) -> None:
        """No-op — virtual device cannot receive hardware commands."""

"""OverflowStrategy — turn a device on when PV surplus exceeds threshold.

Uses hysteresis to avoid rapid on/off cycling near the threshold.

Usage
-----
::

    switch = IoBrokerSwitchAdapter(client, oid="shelly.0.boiler.switch")
    strategy = OverflowStrategy(switch, threshold_w=300, hysteresis_w=50)

    # In the control loop (called every few seconds):
    ctx = ControlContext(surplus_w=live_surplus_w)
    await strategy.execute(ctx)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


@dataclass
class ControlContext:
    """Live system readings passed to a control strategy."""

    surplus_w: float | None = None
    """Available PV surplus in watts (positive = surplus, negative = deficit)."""
    grid_power_w: float | None = None
    pv_power_w: float | None = None
    home_power_w: float | None = None


class OverflowStrategy:
    """Switches a ``Switchable`` on/off based on a PV surplus threshold.

    Turn-on condition:   ``surplus_w >= threshold_w``
    Turn-off condition:  ``surplus_w < threshold_w - hysteresis_w``

    The hysteresis prevents rapid cycling near the threshold.

    Parameters
    ----------
    switch:
        Any object implementing ``turn_on()`` / ``turn_off()`` coroutines
        (i.e. the ``Switchable`` protocol).
    threshold_w:
        Surplus in watts required to turn the device on (default 200 W).
    hysteresis_w:
        Deadband below threshold before turning off (default 50 W).
    """

    def __init__(
        self,
        switch: object,
        threshold_w: float = 200.0,
        hysteresis_w: float = 50.0,
    ) -> None:
        self._switch = switch
        self._threshold_w = threshold_w
        self._hysteresis_w = hysteresis_w
        self._active: bool = False

    async def execute(self, context: ControlContext) -> None:
        """Evaluate current surplus and actuate the switch if needed."""
        surplus = context.surplus_w
        if surplus is None:
            return

        if not self._active and surplus >= self._threshold_w:
            _log.info(
                "OverflowStrategy: surplus %.0f W ≥ %.0f W — turning on",
                surplus,
                self._threshold_w,
            )
            await self._switch.turn_on()
            self._active = True

        elif self._active and surplus < self._threshold_w - self._hysteresis_w:
            _log.info(
                "OverflowStrategy: surplus %.0f W < %.0f W — turning off",
                surplus,
                self._threshold_w - self._hysteresis_w,
            )
            await self._switch.turn_off()
            self._active = False

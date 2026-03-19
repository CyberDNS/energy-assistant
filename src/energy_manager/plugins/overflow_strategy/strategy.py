"""
PV overflow control strategy.

Turns a switchable device on when PV surplus exceeds a configured threshold,
and off when surplus drops below the threshold minus a hysteresis band.  The
dead-band prevents rapid switching when surplus hovers around the threshold.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.control_protocol import ControlContext

log = logging.getLogger(__name__)


class OverflowStrategy:
    """
    Switch a device on/off based on available PV surplus.

    Decision logic
    --------------
    * surplus ≥ threshold_w                  → turn on
    * surplus < threshold_w − hysteresis_w   → turn off
    * otherwise (hysteresis band)            → no change

    Parameters
    ----------
    device:
        A ``Switchable`` device — must implement ``turn_on()`` and
        ``turn_off()``.
    threshold_w:
        Surplus (W) required to switch the device on.  Default 200 W.
    hysteresis_w:
        Dead-band below the on-threshold before switching off (W).
        Default 50 W.
    """

    def __init__(
        self,
        device: Any,
        *,
        threshold_w: float = 200.0,
        hysteresis_w: float = 50.0,
    ) -> None:
        self._device = device
        self._threshold = threshold_w
        self._hysteresis = hysteresis_w

    async def execute(self, context: ControlContext) -> None:
        surplus = context.surplus_w
        if surplus is None:
            return

        off_threshold = self._threshold - self._hysteresis

        if surplus >= self._threshold:
            log.debug(
                "OverflowStrategy: surplus=%.0f W ≥ %.0f W → turn on",
                surplus, self._threshold,
            )
            await self._device.turn_on()
        elif surplus < off_threshold:
            log.debug(
                "OverflowStrategy: surplus=%.0f W < %.0f W → turn off",
                surplus, off_threshold,
            )
            await self._device.turn_off()
        # else: within hysteresis band — no change

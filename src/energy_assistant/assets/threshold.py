"""Threshold-controlled device asset and control contributor.

A threshold device is any on/off load that keeps a measured environmental
value (temperature, humidity, CO₂, …) between two bounds.  Examples:
- Aquarium cooler: maintains water temperature between 24 °C and 28 °C.
- Dehumidifier: maintains room humidity between 40 %RH and 65 %RH.
- Terrarium heater: maintains enclosure temperature between 26 °C and 32 °C.

The MILP optimizer plans *when* to run the device (binary on/off per hour)
so the value stays within bounds while minimising energy cost.

The ``ThresholdControlContributor`` executes each plan slot at the 30-second
control tick.  It adds two safety layers on top of the optimizer's plan:

1. **Emergency override**: if the live measured value reaches a hard boundary
   (at or past a threshold), the device is forced on or off regardless of
   what the plan says.
2. **Compressor protection**: ``min_runtime_h`` and ``min_offtime_h`` prevent
   rapid cycling that would wear out compressor-based devices.

Measured value in DeviceState
------------------------------
The current measured value must be available as a float in
``DeviceState.extra["measured_value"]`` for both the optimizer (initial
condition) and the contributor (emergency logic).  The plugin that reads
the sensor is responsible for populating this field.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..core.models import ThresholdConstraints

if TYPE_CHECKING:
    from ..core.control import ControlIntent, LiveSituation


class ThresholdControlContributor:
    """``ControlContributor`` for a threshold-controlled on/off device.

    Translates optimizer intents (``"run"`` / ``"standby"``) into binary
    power setpoints, with emergency overrides and compressor protection.

    Parameters
    ----------
    constraints:
        Physical limits and rated power of the device.
    """

    def __init__(self, constraints: ThresholdConstraints) -> None:
        self._constraints = constraints
        self._running_since: datetime | None = None
        self._stopped_since: datetime | None = None

    @property
    def device_id(self) -> str:
        return self._constraints.device_id

    def desired_setpoint_w(
        self,
        intent: "ControlIntent | None",
        live: "LiveSituation",
    ) -> float | None:
        """Return rated power (W) when running, 0 W when standby, or None.

        Decision order
        --------------
        1. Emergency override — force on/off when value is at a hard boundary.
        2. Compressor min-runtime — keep running if started too recently.
        3. Compressor min-offtime — stay off if stopped too recently.
        4. Follow the optimizer's intent (``"run"`` or ``"standby"``).
        5. Default to standby when no intent is available.
        """
        tc = self._constraints
        now = live.timestamp
        state = live.device_states.get(self.device_id)

        current_value: float | None = None
        if state is not None and state.available:
            raw = state.extra.get("measured_value")
            if raw is not None:
                current_value = float(raw)

        is_currently_running = (
            state is not None
            and state.power_w is not None
            and state.power_w > tc.rated_power_kw * 500.0  # >50% rated power
        )

        # ── 1. Emergency overrides ────────────────────────────────────
        if current_value is not None:
            if tc.direction == "reduces":
                if current_value >= tc.top_threshold:
                    self._mark_running(now, is_currently_running)
                    return tc.rated_power_kw * 1000.0
                if current_value <= tc.bottom_threshold:
                    self._mark_stopped(now, is_currently_running)
                    return 0.0
            else:  # "increases"
                if current_value <= tc.bottom_threshold:
                    self._mark_running(now, is_currently_running)
                    return tc.rated_power_kw * 1000.0
                if current_value >= tc.top_threshold:
                    self._mark_stopped(now, is_currently_running)
                    return 0.0

        wants_to_run = intent is not None and intent.mode == "run"

        # ── 2. Compressor min-runtime ─────────────────────────────────
        if (
            is_currently_running
            and not wants_to_run
            and tc.min_runtime_h > 0.0
            and self._running_since is not None
        ):
            elapsed_h = (now - self._running_since).total_seconds() / 3600.0
            if elapsed_h < tc.min_runtime_h:
                return tc.rated_power_kw * 1000.0

        # ── 3. Compressor min-offtime ─────────────────────────────────
        if (
            not is_currently_running
            and wants_to_run
            and tc.min_offtime_h > 0.0
            and self._stopped_since is not None
        ):
            elapsed_h = (now - self._stopped_since).total_seconds() / 3600.0
            if elapsed_h < tc.min_offtime_h:
                return 0.0

        # ── 4. Follow intent / 5. Default standby ────────────────────
        if wants_to_run:
            self._mark_running(now, is_currently_running)
            return tc.rated_power_kw * 1000.0

        self._mark_stopped(now, is_currently_running)
        return 0.0

    def charge_price_eur_per_kwh(
        self,
        intent: "ControlIntent | None",
        live: "LiveSituation",
    ) -> float:
        return live.market_price_eur_per_kwh

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _mark_running(self, now: datetime, was_already_running: bool) -> None:
        if not was_already_running:
            self._running_since = now
            self._stopped_since = None

    def _mark_stopped(self, now: datetime, was_running: bool) -> None:
        if was_running:
            self._stopped_since = now
            self._running_since = None

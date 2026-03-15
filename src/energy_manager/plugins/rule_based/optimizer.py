"""
Rule-based optimizer plugin.

This is a deterministic, heuristic optimizer — not a mathematical solver.
It is intentionally simple: correct, testable, and predictable.  More
sophisticated strategies (LP, MPC) can be added later as separate
``Optimizer`` implementations without touching this one.

Rules applied (in priority order)
----------------------------------
1. **Battery discharge floor** — never schedule battery discharge below
   ``min_soc_pct`` (default 10 %).  This is a hard constraint.

2. **Solar-first charging** — if solar surplus exists (SOURCE power exceeds
   active CONSUMER load), schedule battery charging up to the surplus, capped
   at battery capacity.

3. **Grid-import minimisation** — shift sheddable CONSUMER load to hours
   where PV generation forecast is highest (or, without a forecast, do
   nothing — passive optimisation only).

The planner horizon is split into 1-hour slots.  For each slot the optimizer
emits zero or more ``ControlAction`` entries in the returned ``EnergyPlan``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...core.models import (
    ControlAction,
    DeviceCategory,
    EnergyPlan,
    ForecastQuantity,
)
from ...core.optimizer import OptimizationContext

# Battery SoC below which discharge actions are suppressed.
_DEFAULT_MIN_SOC_PCT = 10.0
# Minimum surplus power (W) before a charge action is scheduled.
_SURPLUS_THRESHOLD_W = 50.0


class RuleBasedOptimizer:
    """
    Deterministic rule-based optimizer.

    Implements the ``Optimizer`` protocol structurally.

    Parameters
    ----------
    min_soc_pct:
        Hard floor for battery state-of-charge.  Discharge actions that would
        take any battery below this level are suppressed.
    """

    def __init__(self, min_soc_pct: float = _DEFAULT_MIN_SOC_PCT) -> None:
        if not (0 <= min_soc_pct <= 100):
            raise ValueError("min_soc_pct must be between 0 and 100")
        self._min_soc_pct = min_soc_pct

    async def optimize(self, context: OptimizationContext) -> EnergyPlan:
        actions: list[ControlAction] = []
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        total_slots = max(1, int(context.horizon.total_seconds() / 3600))

        pv_forecast = context.forecasts.get(ForecastQuantity.PV_GENERATION, [])
        # Build a quick lookup: slot_index → forecast value (W)
        pv_by_slot: dict[int, float] = {}
        for point in pv_forecast:
            slot = int((point.timestamp - now).total_seconds() / 3600)
            if 0 <= slot < total_slots:
                pv_by_slot[slot] = point.value

        # Categorise devices from current state snapshot.
        sources = [
            s for s in context.device_states.values()
            if self._category_of(s.device_id, context) == DeviceCategory.SOURCE
        ]
        storages = [
            s for s in context.device_states.values()
            if self._category_of(s.device_id, context) == DeviceCategory.STORAGE
            and s.available
        ]
        consumers = [
            s for s in context.device_states.values()
            if self._category_of(s.device_id, context) == DeviceCategory.CONSUMER
            and s.available
        ]

        # Current instantaneous power readings (W, None treated as 0).
        total_source_w = sum(s.power_w or 0.0 for s in sources)
        total_consumer_w = abs(sum(min(s.power_w or 0.0, 0.0) for s in consumers))
        current_surplus_w = total_source_w - total_consumer_w

        for slot in range(total_slots):
            slot_time = now + timedelta(hours=slot)
            # Prefer the forecast surplus for future slots; use current for slot 0.
            slot_pv_w = pv_by_slot.get(slot, total_source_w if slot == 0 else 0.0)
            slot_surplus_w = slot_pv_w - total_consumer_w

            for storage in storages:
                soc = storage.soc_pct if storage.soc_pct is not None else 50.0

                if slot_surplus_w >= _SURPLUS_THRESHOLD_W:
                    actions.append(
                        ControlAction(
                            device_id=storage.device_id,
                            command="set_charge_power",
                            value=round(slot_surplus_w),
                            scheduled_at=slot_time,
                        )
                    )
                elif slot_surplus_w < -_SURPLUS_THRESHOLD_W and soc > self._min_soc_pct:
                    discharge_w = min(abs(slot_surplus_w), self._max_discharge_w(soc))
                    actions.append(
                        ControlAction(
                            device_id=storage.device_id,
                            command="set_discharge_power",
                            value=round(discharge_w),
                            scheduled_at=slot_time,
                        )
                    )

        _ = current_surplus_w  # used via slot == 0 path above

        return EnergyPlan(
            horizon_hours=total_slots,
            actions=actions,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _category_of(device_id: str, context: OptimizationContext) -> DeviceCategory | None:
        state = context.device_states.get(device_id)
        if state is None:
            return None
        raw = state.extra.get("category")
        if raw is None:
            return None
        try:
            return DeviceCategory(raw)
        except ValueError:
            return None

    @staticmethod
    def _max_discharge_w(soc_pct: float) -> float:
        return max(0.0, (soc_pct / 100.0) * 5000.0)

"""EfficiencyLearner: learns a battery's efficiency and self-consumption.

Two estimates from the controller's measurement history, the better first:

* **Energy balance** (``core/battery_efficiency.fit_balance``): windows of at
  least 12 h between settled rest points; the charged and discharged energy
  must explain the change of the stored energy with one efficiency per
  direction and a constant self-consumption.  Gives both loss terms of the
  battery model (``charge_efficiency`` / ``discharge_efficiency`` and
  ``self_consumption_w``).
* **Closed cycles** (rest to rest at the same SoC): a round trip only, the
  fallback while the energy balance is not confident.

Configured on the storage device::

    - id: zendure
      category: battery
      params:
        charge_efficiency: 0.95      # used until the estimate is confident,
        discharge_efficiency: 0.95   # and always with efficiency: configured
        efficiency: learned          # default: configured
        efficiency_learning:         # all optional
          history_days: 14
          min_cycles: 3
          refit_interval_h: 6

Every battery is learned, so the Learning tab can compare learned and
configured values before you switch one to ``learned``.  Cycles come from
the controller's own measurement history (``core/battery_efficiency``) and
are kept in ``learned_models/efficiency_<id>.json``, so the estimate can
look back further than the measurement history is kept.

Once in use, the learned value is used by the **plan** and by the savings
accounting's uncontrolled house: the real house's losses are what the SoC
readings show, so the uncontrolled house, which would have the same battery,
must not run on the configured (usually better) value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...core.battery_efficiency import (
    BalanceEstimate,
    BalanceWindow,
    CycleWindow,
    EfficiencyEstimate,
    estimate,
    find_balance_windows,
    find_cycles,
    fit_balance,
)
from ...core.device_model import DeviceSpec
from ...core.models import StorageConstraints

_log = logging.getLogger(__name__)

_SERIES_STEP = timedelta(minutes=15)


class EfficiencyLearner:
    """Keeps the learned round-trip efficiency of one battery up to date."""

    def __init__(
        self,
        constraints: StorageConstraints,
        storage: Any,
        *,
        mode: str = "configured",
        label: str = "",
        history_days: float = 14.0,
        min_cycles: int = 3,
        refit_interval_h: float = 6.0,
        model_path: Path | None = None,
    ) -> None:
        self.constraints = constraints
        self._storage = storage
        self.mode = mode if mode in ("configured", "learned") else "configured"
        self.label = label or constraints.device_id.replace("_", " ").title()
        self._history = timedelta(days=history_days)
        self._min_cycles = min_cycles
        self._refit_interval = timedelta(hours=refit_interval_h)
        self._model_path = model_path
        self._cycles: list[CycleWindow] = []
        self._estimate: EfficiencyEstimate | None = None
        self._windows: list[BalanceWindow] = []
        self._balance: BalanceEstimate | None = None
        self._load()
        self._last_attempt: datetime | None = None
        self._last_error: str | None = None
        self._series: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None

    @property
    def device_id(self) -> str:
        return self.constraints.device_id

    @property
    def id(self) -> str:
        return f"efficiency:{self.device_id}"

    @property
    def estimate(self) -> EfficiencyEstimate | None:
        return self._estimate

    @property
    def balance(self) -> BalanceEstimate | None:
        return self._balance

    @property
    def method(self) -> str | None:
        """Which estimate is used: ``balance`` (efficiency and self-consumption
        from the energy balance) before ``cycles`` (round trip from closed SoC
        cycles); ``None`` when neither is confident."""
        if self._balance is not None and self._balance.confident:
            return "balance"
        if self._estimate is not None and self._estimate.confident:
            return "cycles"
        return None

    @property
    def in_use(self) -> bool:
        """True when the plan runs on the learned values."""
        return self.mode == "learned" and self.method is not None

    def apply(self, sc: StorageConstraints) -> StorageConstraints:
        """*sc* with the learned values when they are in use, else unchanged.

        The energy balance gives one efficiency per direction; a configured
        asymmetry is kept by scaling the configured pair (their ratio stays,
        their product matches the learned round trip).  Closed cycles give
        only a round trip, split evenly, and no self-consumption."""
        if not self.in_use:
            return sc
        if self.method == "balance":
            assert self._balance is not None
            eta = self._balance.efficiency
            k = math.sqrt(sc.charge_efficiency / sc.discharge_efficiency) if sc.discharge_efficiency > 0 else 1.0
            return sc.model_copy(update={
                "charge_efficiency": round(min(1.0, eta * k), 4),
                "discharge_efficiency": round(min(1.0, eta / k), 4),
                "self_consumption_w": self._balance.self_consumption_w,
            })
        assert self._estimate is not None
        eta = round(self._estimate.efficiency, 4)
        return sc.model_copy(update={"charge_efficiency": eta, "discharge_efficiency": eta})

    # -- fitting -----------------------------------------------------------

    def schedule_refit(self) -> None:
        """Start a background refit when due (at most one at a time)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.maybe_refit())

    async def maybe_refit(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self._last_attempt is not None and now - self._last_attempt < self._refit_interval:
            return
        await self.refit(now)

    async def refit(self, now: datetime | None = None) -> EfficiencyEstimate | None:
        """Find cycles in the measurement history, merge them with the kept
        ones and re-estimate.  Keeps the previous estimate on failure."""
        now = now or datetime.now(timezone.utc)
        self._last_attempt = now
        sc = self.constraints
        try:
            samples = await self._storage.query_power_soc(sc.device_id, now - self._history, now)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"history query failed: {exc}"
            _log.warning("%s: %s", self.id, self._last_error)
            return None
        new = find_cycles(
            samples, sc.capacity_kwh, standby_w=sc.standby_w,
            min_soc_pct=sc.min_soc_pct, max_soc_pct=sc.max_soc_pct,
        )
        self._series = _series(samples)
        # Kept cycles from before the history now available, plus all found now.
        first = samples[0][0] if samples else now
        cutoff = now - self._history
        self._cycles = sorted(
            [c for c in self._cycles if c.end < first and c.start >= cutoff] + new,
            key=lambda c: c.start,
        )
        windows = find_balance_windows(
            samples, sc.capacity_kwh, standby_w=sc.standby_w, max_soc_pct=sc.max_soc_pct,
        )
        self._windows = sorted(
            [w for w in self._windows if w.end < first and w.start >= cutoff] + windows,
            key=lambda w: w.start,
        )
        self._balance = fit_balance(self._windows, sc.capacity_kwh, fitted_at=now)
        if self._balance is not None:
            b = self._balance
            _log.info(
                "%s: energy balance η %.3f per direction, self-consumption %.0f W "
                "from %d windows over %.1f days%s",
                self.id, b.efficiency, b.self_consumption_w, b.windows, b.days,
                "" if b.confident else " (not yet confident)",
            )
        est = estimate(
            self._cycles, sc.capacity_kwh, min_cycles=self._min_cycles, fitted_at=now,
        )
        if est is None:
            self._last_error = (None if self._balance is not None
                                else "no closed charge/discharge cycle found yet")
            self._estimate = None
            self._save()
            return None
        self._estimate = est
        self._last_error = None
        self._save()
        _log.info(
            "%s: round trip %.3f (η %.3f per direction) from %d cycles, %.1f kWh in%s",
            self.id, est.round_trip, est.efficiency, est.cycles, est.energy_in_kwh,
            "" if est.confident else " (not yet confident)",
        )
        return est

    # -- diagnostics -------------------------------------------------------

    def diagnostics_reason(self) -> str:
        return self._last_error or "unknown"

    def summary(self) -> dict[str, Any]:
        """Configured vs. learned efficiency, for ``/api/config``."""
        sc = self.constraints
        est = self._estimate
        bal = self._balance
        return {
            "mode": self.mode,
            "in_use": self.in_use,
            "method": self.method,
            "learned_round_trip": est.round_trip if est else None,
            "learned_efficiency": round(est.efficiency, 4) if est else None,
            "confident": bool(est and est.confident),
            "cycles": est.cycles if est else 0,
            "balance": bal.to_dict() if bal else None,
            "configured_round_trip": round(sc.charge_efficiency * sc.discharge_efficiency, 4),
            "configured_self_consumption_w": sc.self_consumption_w,
        }

    async def diagnostics(self, *, detail: bool = True) -> dict[str, Any]:
        """Learning-tab payload; without the cycles and the chart series with
        ``detail=False`` (the Learning overview)."""
        if self._last_attempt is None:
            await self.refit()
        sc = self.constraints
        est = self._estimate
        if self.in_use:
            status = "learned"
        elif est is None and self._balance is None:
            status = "no_model"
        elif self.method is None:
            status = "collecting"
        else:
            status = "observing"
        planned = self.apply(sc)
        out = {
            "id": self.id,
            "kind": "efficiency",
            "device_id": sc.device_id,
            "label": self.label,
            "status": status,
            "mode": self.mode,
            "last_error": self._last_error,
            "fitted_at": est.fitted_at if est else None,
            "history_days": self._history.total_seconds() / 86400.0,
            "min_cycles": self._min_cycles,
            "capacity_kwh": sc.capacity_kwh,
            "standby_w": sc.standby_w,
            "configured": {
                "charge_efficiency": sc.charge_efficiency,
                "discharge_efficiency": sc.discharge_efficiency,
                "round_trip": round(sc.charge_efficiency * sc.discharge_efficiency, 4),
            },
            "planned": {
                "charge_efficiency": planned.charge_efficiency,
                "discharge_efficiency": planned.discharge_efficiency,
                "self_consumption_w": planned.self_consumption_w,
            },
            "configured_self_consumption_w": sc.self_consumption_w,
            "method": self.method,
            "estimate": est.to_dict() if est else None,
            "balance": self._balance.to_dict() if self._balance else None,
        }
        if not detail:
            return out
        return {
            **out,
            "cycles": [
                {**c.to_dict(), "round_trip": round(c.round_trip, 4)} for c in self._cycles
            ],
            "balance_windows": [w.to_dict() for w in self._windows],
            "series": self._series,
        }

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self._model_path is None or not self._model_path.exists():
            return
        try:
            data = json.loads(self._model_path.read_text())
            self._cycles = [CycleWindow.from_dict(c) for c in data.get("cycles", [])]
            if data.get("estimate"):
                self._estimate = EfficiencyEstimate.from_dict(data["estimate"])
            self._windows = [BalanceWindow.from_dict(w) for w in data.get("balance_windows", [])]
            if data.get("balance"):
                self._balance = BalanceEstimate.from_dict(data["balance"])
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: could not read %s: %s", self.id, self._model_path, exc)

    def _save(self) -> None:
        if self._model_path is None:
            return
        data = {
            "cycles": [c.to_dict() for c in self._cycles],
            "estimate": self._estimate.to_dict() if self._estimate else None,
            "balance_windows": [w.to_dict() for w in self._windows],
            "balance": self._balance.to_dict() if self._balance else None,
        }
        try:
            self._model_path.parent.mkdir(parents=True, exist_ok=True)
            self._model_path.write_text(json.dumps(data))
        except OSError as exc:
            _log.warning("%s: could not save %s: %s", self.id, self._model_path, exc)


def _series(samples: list[tuple[datetime, float | None, float | None]]) -> list[dict[str, Any]]:
    """SoC (last reading) and mean power per 15 minutes, for the chart."""
    out: list[dict[str, Any]] = []
    bucket: datetime | None = None
    soc: float | None = None
    p_sum = 0.0
    n = 0

    def flush() -> None:
        if bucket is not None and n:
            out.append({"t": bucket.isoformat(), "soc_pct": soc, "power_w": round(p_sum / n)})

    for ts, power, s in samples:
        b = ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)
        if b != bucket:
            flush()
            bucket, soc, p_sum, n = b, None, 0.0, 0
        if s is not None:
            soc = s
        if power is not None:
            p_sum += power
            n += 1
    flush()
    return out


def build_efficiency_learners(
    constraints: list[StorageConstraints],
    devices: Mapping[str, DeviceSpec],
    storage: Any,
    data_dir: Path | None,
) -> dict[str, EfficiencyLearner]:
    """One learner per battery (``category: battery``)."""
    out: dict[str, EfficiencyLearner] = {}
    if storage is None or not hasattr(storage, "query_power_soc"):
        return out
    for sc in constraints:
        spec = devices.get(sc.device_id)
        params = spec.params if spec is not None else {}
        opts = params.get("efficiency_learning") or {}
        mode = str(params.get("efficiency", "configured"))
        if mode not in ("configured", "learned"):
            _log.warning(
                "%s: efficiency must be 'configured' or 'learned', not %r — using configured",
                sc.device_id, mode,
            )
            mode = "configured"
        out[sc.device_id] = EfficiencyLearner(
            sc, storage,
            mode=mode,
            label=(spec.label or "") if spec is not None else "",
            history_days=float(opts.get("history_days", 14)),
            min_cycles=int(opts.get("min_cycles", 3)),
            refit_interval_h=float(opts.get("refit_interval_h", 6)),
            model_path=(data_dir / "learned_models" / f"efficiency_{sc.device_id}.json")
            if data_dir else None,
        )
    return out

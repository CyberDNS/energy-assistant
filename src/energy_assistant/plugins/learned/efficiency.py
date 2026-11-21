"""EfficiencyLearner: learns a battery's effective round-trip efficiency.

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

Only the **plan** uses the learned value.  The savings accounting keeps the
configured values: both of its houses run on the same battery, and a
battery doing worse than configured should show as lower savings, not be
hidden by them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...core.battery_efficiency import (
    CycleWindow,
    EfficiencyEstimate,
    estimate,
    find_cycles,
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
    def in_use(self) -> bool:
        """True when the plan runs on the learned value."""
        return self.mode == "learned" and self._estimate is not None and self._estimate.confident

    def apply(self, sc: StorageConstraints) -> StorageConstraints:
        """*sc* with the learned efficiency when it is in use, else unchanged."""
        if not self.in_use:
            return sc
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
        est = estimate(
            self._cycles, sc.capacity_kwh, min_cycles=self._min_cycles, fitted_at=now,
        )
        if est is None:
            self._last_error = "no closed charge/discharge cycle found yet"
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
        return {
            "mode": self.mode,
            "in_use": self.in_use,
            "learned_round_trip": est.round_trip if est else None,
            "learned_efficiency": round(est.efficiency, 4) if est else None,
            "confident": bool(est and est.confident),
            "cycles": est.cycles if est else 0,
            "configured_round_trip": round(sc.charge_efficiency * sc.discharge_efficiency, 4),
        }

    async def diagnostics(self) -> dict[str, Any]:
        """Learning-tab payload."""
        if self._last_attempt is None:
            await self.refit()
        sc = self.constraints
        est = self._estimate
        if self.in_use:
            status = "learned"
        elif est is None:
            status = "no_model"
        elif not est.confident:
            status = "collecting"
        else:
            status = "observing"
        planned = self.apply(sc)
        return {
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
            },
            "estimate": est.to_dict() if est else None,
            "cycles": [
                {**c.to_dict(), "round_trip": round(c.round_trip, 4)} for c in self._cycles
            ],
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
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: could not read %s: %s", self.id, self._model_path, exc)

    def _save(self) -> None:
        if self._model_path is None:
            return
        data = {
            "cycles": [c.to_dict() for c in self._cycles],
            "estimate": self._estimate.to_dict() if self._estimate else None,
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

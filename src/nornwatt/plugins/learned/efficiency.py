"""EfficiencyLearner: learns a battery's efficiency and self-consumption.

From the controller's own measurement history (AC power and SoC per
5 minutes), the energy balance (``core/battery_efficiency``) fits both loss
terms of the battery model: one efficiency per direction and a constant
self-consumption (``self_consumption_w``).

Configured on the storage device::

    - id: zendure
      category: battery
      params:
        charge_efficiency: 0.95      # used until the estimate is confident,
        discharge_efficiency: 0.95   # and always with efficiency: configured
        self_consumption_w: 0
        efficiency: learned          # default: configured
        efficiency_learning:         # all optional
          history_days: 90
          refit_interval_h: 6

Every battery is learned, so the Learning tab can compare learned and
configured values before you switch one to ``learned``.  The windows are
kept in ``learned_models/efficiency_<id>.json``, so the estimate can look
back further than the measurement history is kept.

Once in use, the learned values are used by the **plan** and by the savings
accounting's uncontrolled house: the real house's losses are what the SoC
readings show, so the uncontrolled house, which would have the same battery,
must not run on the configured (usually better) values.
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
    find_balance_windows,
    find_energy_windows,
    fit_balance,
)
from ...core.device_model import DeviceSpec
from ...core.models import StorageConstraints

_log = logging.getLogger(__name__)

ENERGY_HISTORY = timedelta(days=730)
"""How far back power without SoC (imported) is used: month-long windows
need many months, and the import reaches two years back."""


class EfficiencyLearner:
    """Keeps the learned efficiency and self-consumption of one battery up to date."""

    def __init__(
        self,
        constraints: StorageConstraints,
        storage: Any,
        *,
        mode: str = "configured",
        label: str = "",
        history_days: float = 90.0,
        refit_interval_h: float = 6.0,
        model_path: Path | None = None,
    ) -> None:
        self.constraints = constraints
        self._storage = storage
        self.mode = mode if mode in ("configured", "learned") else "configured"
        self.label = label or constraints.device_id.replace("_", " ").title()
        self._history = timedelta(days=history_days)
        self._refit_interval = timedelta(hours=refit_interval_h)
        self._model_path = model_path
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
    def balance(self) -> BalanceEstimate | None:
        return self._balance

    @property
    def confident(self) -> bool:
        return self._balance is not None and self._balance.confident

    @property
    def in_use(self) -> bool:
        """True when the plan runs on the learned values."""
        return self.mode == "learned" and self.confident

    def apply(self, sc: StorageConstraints) -> StorageConstraints:
        """*sc* with the learned values when they are in use, else unchanged.

        The energy balance gives one efficiency per direction; a configured
        asymmetry is kept by scaling the configured pair (their ratio stays,
        their product matches the learned round trip)."""
        if not self.in_use:
            return sc
        assert self._balance is not None
        eta = self._balance.efficiency
        k = math.sqrt(sc.charge_efficiency / sc.discharge_efficiency) if sc.discharge_efficiency > 0 else 1.0
        return sc.model_copy(update={
            "charge_efficiency": round(min(1.0, eta * k), 4),
            "discharge_efficiency": round(min(1.0, eta / k), 4),
            "self_consumption_w": self._balance.self_consumption_w,
        })

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

    async def refit(self, now: datetime | None = None) -> BalanceEstimate | None:
        """Find windows in the measurement history, merge them with the kept
        ones and re-fit.  Keeps the previous estimate on failure."""
        now = now or datetime.now(timezone.utc)
        self._last_attempt = now
        sc = self.constraints
        try:
            if hasattr(self._storage, "query_learning_history"):
                # Hourly rows (measured or imported from Home Assistant) up to
                # where the 5-minute rows begin, then the 5-minute rows; power
                # without SoC counts in month-long windows, so it is read
                # further back (ENERGY_HISTORY).
                samples = await self._storage.query_learning_history(
                    sc.device_id, now - max(self._history, ENERGY_HISTORY), now)
            else:
                samples = await self._storage.query_power_soc(sc.device_id, now - self._history, now)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"history query failed: {exc}"
            _log.warning("%s: %s", self.id, self._last_error)
            return None
        recent = [s for s in samples if s[0] >= now - self._history]
        self._series = _series(recent)
        windows = find_balance_windows(
            recent, sc.capacity_kwh, standby_w=sc.standby_w, max_soc_pct=sc.max_soc_pct,
        ) + find_energy_windows(samples, standby_w=sc.standby_w)
        # Kept windows from before the history now available, plus all found now.
        first = samples[0][0] if samples else now
        cutoff = now - self._history
        self._windows = sorted(
            [w for w in self._windows if w.end < first
             and w.start >= (cutoff if w.soc_known else now - ENERGY_HISTORY)] + windows,
            key=lambda w: w.start,
        )
        self._balance = fit_balance(self._windows, sc.capacity_kwh, fitted_at=now)
        if self._balance is None:
            self._last_error = "not enough rest-to-rest windows yet"
        else:
            self._last_error = None
            b = self._balance
            _log.info(
                "%s: η %.3f per direction, self-consumption %.0f W "
                "from %d windows over %.1f days%s",
                self.id, b.efficiency, b.self_consumption_w, b.windows, b.days,
                "" if b.confident else " (not yet confident)",
            )
        self._save()
        return self._balance

    # -- diagnostics -------------------------------------------------------

    def diagnostics_reason(self) -> str:
        return self._last_error or "unknown"

    def summary(self) -> dict[str, Any]:
        """Configured vs. learned values, for ``/api/config``."""
        sc = self.constraints
        bal = self._balance
        return {
            "mode": self.mode,
            "in_use": self.in_use,
            "confident": self.confident,
            "learned_round_trip": round(bal.round_trip, 4) if bal else None,
            "learned_efficiency": bal.efficiency if bal else None,
            "learned_self_consumption_w": bal.self_consumption_w if bal else None,
            "balance": bal.to_dict() if bal else None,
            "configured_round_trip": round(sc.charge_efficiency * sc.discharge_efficiency, 4),
            "configured_self_consumption_w": sc.self_consumption_w,
        }

    async def diagnostics(self, *, detail: bool = True) -> dict[str, Any]:
        """Learning-tab payload; without the windows and the chart series with
        ``detail=False`` (the Learning overview)."""
        if self._last_attempt is None:
            await self.refit()
        sc = self.constraints
        bal = self._balance
        if self.in_use:
            status = "learned"
        elif bal is None:
            status = "no_model"
        elif not bal.confident:
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
            "fitted_at": bal.fitted_at if bal else None,
            "history_days": self._history.total_seconds() / 86400.0,
            "capacity_kwh": sc.capacity_kwh,
            "standby_w": sc.standby_w,
            "configured": {
                "charge_efficiency": sc.charge_efficiency,
                "discharge_efficiency": sc.discharge_efficiency,
                "round_trip": round(sc.charge_efficiency * sc.discharge_efficiency, 4),
                "self_consumption_w": sc.self_consumption_w,
            },
            "planned": {
                "charge_efficiency": planned.charge_efficiency,
                "discharge_efficiency": planned.discharge_efficiency,
                "self_consumption_w": planned.self_consumption_w,
            },
            "balance": bal.to_dict() if bal else None,
        }
        if not detail:
            return out
        return {
            **out,
            "windows": [w.to_dict() for w in self._windows],
            "series": self._series,
        }

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self._model_path is None or not self._model_path.exists():
            return
        try:
            data = json.loads(self._model_path.read_text())
            self._windows = [BalanceWindow.from_dict(w) for w in data.get("balance_windows", [])]
            if data.get("balance"):
                self._balance = BalanceEstimate.from_dict(data["balance"])
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: could not read %s: %s", self.id, self._model_path, exc)

    def _save(self) -> None:
        if self._model_path is None:
            return
        data = {
            "balance_windows": [w.to_dict() for w in self._windows],
            "balance": self._balance.to_dict() if self._balance else None,
        }
        try:
            self._model_path.parent.mkdir(parents=True, exist_ok=True)
            self._model_path.write_text(json.dumps(data))
        except OSError as exc:
            _log.warning("%s: could not save %s: %s", self.id, self._model_path, exc)


def _series(samples: list[tuple]) -> list[dict[str, Any]]:
    """SoC (last reading) and mean power per 15 minutes, for the chart."""
    out: list[dict[str, Any]] = []
    bucket: datetime | None = None
    soc: float | None = None
    p_sum = 0.0
    n = 0

    def flush() -> None:
        if bucket is not None and n:
            out.append({"t": bucket.isoformat(), "soc_pct": soc, "power_w": round(p_sum / n)})

    for ts, power, s, *_ in samples:
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
            history_days=float(opts.get("history_days", 90)),
            refit_interval_h=float(opts.get("refit_interval_h", 6)),
            model_path=(data_dir / "learned_models" / f"efficiency_{sc.device_id}.json")
            if data_dir else None,
        )
    return out

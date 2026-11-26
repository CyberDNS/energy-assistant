"""Projected savings — what control would have saved for observed devices.

A device in observe mode (``control: observe``, issue #134) is planned like
any other, and the control loop computes the setpoint it would send, but
nothing is sent: the device keeps its own logic.  Its measured power is
therefore the *uncontrolled* behaviour, and the ordinary savings
accounting (``core/savings``) shows no saving for it.

``ObservedProjection`` turns each live sample of the real site into the
sample of the same site with every observed device following the control
loop instead:

* its power is the setpoint in force since the previous sample (computed on
  the previous control tick), clamped to the device's
  limits — a battery to its charge/discharge power and SoC range, a car to
  its charger's maximum while plugged in, a threshold device to on/off;
  ticks without a setpoint ("no command") keep the measured power;
* the grid connection changes by the same amount (the household load, PV
  and the other devices are as measured);
* an observed battery's stored energy is integrated from the projected
  power (with its efficiencies), starting from the measured SoC.

A second ``SavingsTracker`` runs on these samples: its "real" house is the
planned-control house, its uncontrolled house the usual simulation, so its
slots are the *projected* savings.  Devices in active mode are as measured
in both trackers.

Simplifications (the projection is labelled as simulated in the UI): the
setpoints are computed on the real, not the projected, situation (SoC,
grid power); a car's or threshold device's measured value (SoC,
temperature) is not re-simulated — their energy is matched FIFO against
the uncontrolled house as in the ordinary accounting, so only energy both
houses used is booked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from .models import StorageConstraints, ThresholdConstraints
from .savings import RESET_AFTER_GAP, DeviceLive, EvLive, LiveSample


class ObservedProjection:
    """Live sample of the real site → sample with observed devices controlled."""

    def __init__(
        self,
        batteries: list[StorageConstraints],
        devices: list[ThresholdConstraints] = (),  # type: ignore[assignment]
        state: Mapping[str, Any] | None = None,
    ) -> None:
        self._batteries = {sc.device_id: sc for sc in batteries}
        self._devices = {tc.device_id: tc for tc in devices}
        state = state or {}
        # Projected stored energy per observed battery (kWh).
        self.stored: dict[str, float] = {
            k: float(v) for k, v in (state.get("stored") or {}).items()
        }
        self._last_ts: datetime | None = None

    def set_batteries(self, batteries: list[StorageConstraints]) -> None:
        self._batteries = {sc.device_id: sc for sc in batteries}

    def to_dict(self) -> dict[str, Any]:
        return {"stored": dict(self.stored)}

    def project(
        self,
        sample: LiveSample,
        observed: set[str] | frozenset[str],
        setpoints: Mapping[str, float | None],
    ) -> LiveSample:
        """*sample* with the observed devices following the control loop.

        *setpoints* are the (would-send) setpoints in force since the
        previous sample, ``None`` = no command.  Devices that are not
        observed are left as measured.
        """
        previous, self._last_ts = self._last_ts, sample.timestamp
        hours = (sample.timestamp - previous).total_seconds() / 3600.0 if previous else 0.0
        if previous is None or hours <= 0 or hours > RESET_AFTER_GAP.total_seconds() / 3600.0:
            hours = 0.0
            # Back level with reality after a gap (or at the start).
            self.stored = {}
        in_force = setpoints

        delta_kw = 0.0
        storage_kw = dict(sample.storage_kw)
        stored_kwh = dict(sample.stored_kwh)
        for device_id, sc in self._batteries.items():
            if device_id not in observed:
                self.stored.pop(device_id, None)
                continue
            measured_kw = sample.storage_kw.get(device_id)
            measured_stored = sample.stored_kwh.get(device_id)
            if measured_kw is None or measured_stored is None:
                continue
            stored = self.stored.get(device_id, measured_stored)
            sp = in_force.get(device_id)
            kw = measured_kw if sp is None else sp / 1000.0
            kw = min(max(kw, -sc.max_discharge_kw), sc.max_charge_kw)
            low = sc.capacity_kwh * sc.min_soc_pct / 100.0
            high = sc.capacity_kwh * sc.max_soc_pct / 100.0
            if hours > 0:
                if kw > 0:
                    room = max(0.0, high - stored)
                    kw = min(kw, room / (hours * sc.charge_efficiency))
                    stored += kw * hours * sc.charge_efficiency
                elif kw < 0:
                    avail = max(0.0, stored - low)
                    kw = -min(-kw, avail * sc.discharge_efficiency / hours)
                    stored += kw * hours / sc.discharge_efficiency
            self.stored[device_id] = stored
            storage_kw[device_id] = kw
            stored_kwh[device_id] = stored
            delta_kw += kw - measured_kw

        ev = dict(sample.ev)
        for device_id, live in sample.ev.items():
            sp = in_force.get(device_id)
            if device_id not in observed or sp is None or not live.plugged:
                continue
            kw = min(max(0.0, sp / 1000.0), live.max_kw)
            ev[device_id] = EvLive(plugged=live.plugged, power_kw=kw, max_kw=live.max_kw)
            delta_kw += kw - live.power_kw

        devices = dict(sample.devices)
        for device_id, live in sample.devices.items():
            sp = in_force.get(device_id)
            tc = self._devices.get(device_id)
            if device_id not in observed or sp is None or tc is None or live.power_kw is None:
                continue
            kw = tc.rated_power_kw if sp > 0 else 0.0
            devices[device_id] = DeviceLive(power_kw=kw, value=live.value)
            delta_kw += kw - live.power_kw

        return replace(
            sample,
            site_net_kw=sample.site_net_kw + delta_kw,
            storage_kw=storage_kw,
            stored_kwh=stored_kwh,
            ev=ev,
            devices=devices,
        )

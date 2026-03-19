"""
Energy controller daemon.

Runs two concurrent async loops:

  plan_loop    — re-solves the MILP every ``plan_interval_s`` seconds
                 (default 3600) and snapshots the SoC at the start of each
                 planning cycle.

  control_loop — every ``control_interval_s`` seconds (default 15), reads
                 real-time power sensors from ioBroker and dispatches a
                 charge / discharge setpoint to the Zendure battery.

The control logic is a direct port of the production ioBroker JavaScript
controller.  The decision hierarchy per 15-second tick is:

  1. Maintenance charging  — SoC < 5 % → charge at maintenance_charge_w
  2. Planned discharge     — execute MILP discharge target, adjusted for
                             real-time household load and PV production
  3. Max SoC reached       — idle
  4. Planned charge        — execute MILP charge target, topped up by any
                             PV overflow above the minimum required power
  5. No plan / idle        — charge from PV overflow (if any) else idle

Run with:
    python scripts/run_controller.py

Add  --dry-run  to compute setpoints but skip sending them to the device.
Configuration is loaded from config.yaml (gitignored).
Copy config.yaml.example → config.yaml and fill in your values.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import uvicorn

from energy_manager.core.models import DeviceState, EnergyPlan, ForecastQuantity
from energy_manager.core.optimizer import OptimizationContext
from energy_manager.plugins._iobroker.client import IoBrokerClient
from energy_manager.plugins.milp.optimizer import MILPOptimizer
from energy_manager.plugins.pvforecast_iobroker.forecast import PVForecastIoBrokerForecast
from energy_manager.plugins.home_power_iobroker.device import HomePowerIoBrokerDevice
from energy_manager.plugins.sma_em_iobroker.device import SMAEMIoBrokerDevice
from energy_manager.plugins.sma_modbus_iobroker.device import SMAModbusIoBrokerDevice
from energy_manager.plugins.tibber_iobroker.tariff import TibberIoBrokerTariff
from energy_manager.plugins.tibber_iobroker.live_power import TibberLivePowerIoBrokerDevice
from energy_manager.plugins.zendure_iobroker.device import ZendureIoBrokerDevice
from energy_manager.plugins._homeassistant.client import HAClient
from energy_manager.plugins.mt175_ha.device import MT175HADevice
from energy_manager.secrets import SecretsManager
from energy_manager.core.integration_loader import load_integrations
from energy_manager.core.control_protocol import ControlContext
from energy_manager.server.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
with open(_ROOT / "config.yaml") as _f:
    _cfg = yaml.safe_load(_f)

HOST = _cfg["iobroker"]["host"]
PORT = int(_cfg["iobroker"]["port"])
HOME_ID = _cfg["tibber_iobroker"]["home_id"]
HUB_ID = _cfg["zendure_iobroker"]["hub_id"]
DEVICE_SERIAL = _cfg["zendure_iobroker"]["device_serial"]
CAPACITY_KWH = float(_cfg["zendure_iobroker"]["capacity_kwh"])
MAX_CHARGE_KW = float(_cfg["zendure_iobroker"]["max_charge_kw"])
MAX_DISCHARGE_KW = float(_cfg["zendure_iobroker"]["max_discharge_kw"])

_opt = _cfg.get("milp", {})
HORIZON_H = int(_opt.get("horizon_hours", 24))
BASELINE_LOAD_KW = float(_opt.get("baseline_load_kw", 0.3))
FEED_IN_EUR_PER_KWH = float(_opt.get("feed_in_eur_per_kwh", 0.0))

_pv = _cfg.get("pvforecast_iobroker", {})
PVFORECAST_PLANT_ID = _pv.get("plant_id", "pv")
PVFORECAST_TZ = ZoneInfo(_pv.get("timezone", "Europe/Berlin"))

_ctrl = _cfg.get("controller", {})
CONTROL_INTERVAL_S: int = int(_ctrl.get("control_interval_s", 15))
PLAN_INTERVAL_S: int = int(_ctrl.get("plan_interval_s", 3600))

SERVER_PORT: int = int((_cfg.get("server") or {}).get("port", 8088))

_hp_cfg = _cfg.get("home_power_iobroker") or {}
_em_cfg = _cfg.get("sma_em_iobroker") or {}
_mt175_cfg = _cfg.get("mt175_ha") or {}
_ha_cfg = _cfg.get("homeassistant") or {}

_secrets = SecretsManager(_ROOT / "secrets.yaml")

# Battery constants derived from config
CAPACITY_WH: float = CAPACITY_KWH * 1000
MAX_CHARGE_W: float = MAX_CHARGE_KW * 1000
MAX_DISCHARGE_W: float = MAX_DISCHARGE_KW * 1000

# Maintenance mode thresholds (fixed battery characteristics)
_MIN_SOC_WH: float = CAPACITY_WH * 0.05   # below this → enter maintenance mode
_MAINT_SOC_WH: float = CAPACITY_WH * 0.10  # above this → leave maintenance mode
_MAX_SOC_WH: float = CAPACITY_WH * 0.99   # at or above this → idle (full)

# Charge efficiency used to compensate for roundtrip losses
_CHARGE_EFFICIENCY: float = 0.95

# ---------------------------------------------------------------------------
# Shared controller state (mutated by both loops)
# ---------------------------------------------------------------------------


class ControllerState:
    """
    Mutable state shared between plan_loop and control_loop.

    Attributes
    ----------
    plan:
        Most recent EnergyPlan from the MILP optimizer.  None until the
        first solve completes.
    soc_at_hour_start_wh:
        Battery SoC in Wh at the start of the current hour slot.  Updated
        by control_loop on every hour boundary; used to track how much
        energy has been charged / discharged within the slot.
    maintenance_mode:
        True when SoC dropped below the minimum threshold; cleared when SoC
        recovers to the maintenance threshold.
    _last_input_limit_w / _last_output_limit_w:
        The charge / discharge limits most recently sent to the device.
        Used to compute the adjusted overflow check (matching JS behaviour).
    _last_hour:
        Most recent clock hour seen by control_loop; used to detect hour
        boundary crossings for SoC snapshotting.
    """

    def __init__(self, initial_soc_wh: float) -> None:
        self.plan: EnergyPlan | None = None
        self.soc_at_hour_start_wh: float = initial_soc_wh
        self.maintenance_mode: bool = False
        self._last_input_limit_w: float = 0.0
        self._last_output_limit_w: float = 0.0
        self._last_hour: int = -1
        # Cached device snapshots for the web UI (updated by the loops)
        self.last_zendure_state: DeviceState | None = None
        self.last_sma_state: DeviceState | None = None
        self.last_home_power_state: DeviceState | None = None
        self.last_sma_em_state: DeviceState | None = None
        self.last_tibber_live_state: DeviceState | None = None
        self.last_mt175_state: DeviceState | None = None
        self.registry = None  # set to IntegrationRegistry after construction
        self.last_mode: str = "starting"
        self.last_target_w: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot_action_w(plan: EnergyPlan | None) -> float | None:
    """
    Return the MILP power setpoint (W) for the current UTC hour slot.

    Positive = discharge target, negative = charge target.
    Returns None when no plan is available or the current time falls
    outside all planned slots.
    """
    if plan is None:
        return None
    now = datetime.now(timezone.utc)
    for action in plan.actions:
        slot_end = action.scheduled_at + timedelta(hours=1)
        if action.scheduled_at <= now < slot_end:
            return float(action.value)
    return None


# ---------------------------------------------------------------------------
# Control step (mirrors ControlCharge() in the JS script)
# ---------------------------------------------------------------------------


async def control_step(
    battery: ZendureIoBrokerDevice,
    home_power: HomePowerIoBrokerDevice,
    grid_meter: SMAEMIoBrokerDevice | None,
    tibber_live: TibberLivePowerIoBrokerDevice | None,
    mt175: MT175HADevice | None,
    state_ref: ControllerState,
    dry_run: bool,
) -> None:
    """Execute one control iteration: read sensors, decide setpoint, send it."""
    state = await battery.get_state()
    soc_wh = (state.soc_pct or 0.0) / 100.0 * CAPACITY_WH

    # --- Hour-boundary SoC snapshot (mirrors Calculate() in the JS) --------
    current_hour = datetime.now().hour
    if state_ref._last_hour != current_hour:
        state_ref.soc_at_hour_start_wh = soc_wh
        state_ref._last_hour = current_hour
        log.info("New hour — SoC snapshot: %.0f Wh (%.0f%%)", soc_wh, state.soc_pct or 0)

    # --- Maintenance mode hysteresis ----------------------------------------
    if soc_wh < _MIN_SOC_WH:
        state_ref.maintenance_mode = True
    elif soc_wh >= _MAINT_SOC_WH:
        state_ref.maintenance_mode = False

    # --- MILP slot target ---------------------------------------------------
    planned_w = _slot_action_w(state_ref.plan)
    # For a 1-hour slot, W and Wh are numerically equal.
    # negative planned_w → charge target (Wh to store this hour)
    # positive planned_w → discharge target (Wh to deliver this hour)
    planned_charge_wh = max(-(planned_w or 0.0), 0.0)
    planned_discharge_wh = max((planned_w or 0.0), 0.0)

    # --- Real-time power sensors (from Home Assistant via ioBroker hass adapter)
    reads = [home_power.get_state()]
    if grid_meter is not None:
        reads.append(grid_meter.get_state())
    if tibber_live is not None:
        reads.append(tibber_live.get_state())
    if mt175 is not None:
        reads.append(mt175.get_state())
    results = await asyncio.gather(*reads)
    idx = 1
    hp = results[0]
    if grid_meter is not None:
        state_ref.last_sma_em_state = results[idx]
        idx += 1
    if tibber_live is not None:
        state_ref.last_tibber_live_state = results[idx]
        idx += 1
    if mt175 is not None:
        state_ref.last_mt175_state = results[idx]

    household_w = hp.power_w or 0.0
    overflow_w = hp.extra.get("overflow_w") or 0.0
    cars_w = hp.extra.get("cars_w") or 0.0
    pv_w = hp.extra.get("pv_w") or 0.0

    now = datetime.now()
    pct_hour_done = now.minute / 60.0

    # --- Decision hierarchy (platform convention: negative=charge, positive=discharge)
    if state_ref.maintenance_mode and planned_charge_wh < battery.maintenance_charge_w:
        # Battery critically low — charge regardless of plan
        target_w = -battery.maintenance_charge_w
        mode = "maintenance"

    elif planned_discharge_wh > 0:
        already_discharged_wh = max(state_ref.soc_at_hour_start_wh - soc_wh, 0.0)
        remaining_wh = max(planned_discharge_wh - already_discharged_wh, 0.0)

        # Net overflow above what the battery is already delivering
        adjusted_overflow = overflow_w - state_ref._last_output_limit_w
        if adjusted_overflow > 0:
            # PV is producing more than expected — absorb the surplus
            target_w = -adjusted_overflow
            mode = "overflow during planned discharge"
        else:
            remaining_fraction = 1.0 - pct_hour_done
            max_power_w = (
                remaining_wh / remaining_fraction if remaining_fraction > 0.001
                else MAX_DISCHARGE_W
            )
            # Only cover net house load (household − PV); cap at battery limit
            net_house_load_w = max(household_w - pv_w, 0.0)
            actual_w = min(net_house_load_w, max_power_w, MAX_DISCHARGE_W)
            target_w = actual_w  # positive = discharge
            mode = "planned discharge"

    elif soc_wh >= _MAX_SOC_WH:
        target_w = 0.0
        mode = "max SoC"

    elif planned_charge_wh > 0:
        already_charged_wh = max(soc_wh - state_ref.soc_at_hour_start_wh, 0.0)
        remaining_wh = max(planned_charge_wh - already_charged_wh, 0.0)

        remaining_fraction = 1.0 - pct_hour_done
        if remaining_fraction > 0.001:
            # Power needed to deliver remaining energy by end of slot,
            # adjusting for charge efficiency (draw more from grid than stored).
            min_power_w = min(
                remaining_wh / remaining_fraction / _CHARGE_EFFICIENCY,
                MAX_CHARGE_W,
            )
        else:
            min_power_w = 0.0

        # Overflow above what we're already charging adds for free
        adjusted_overflow = overflow_w + state_ref._last_input_limit_w
        actual_w = max(min_power_w, adjusted_overflow) if adjusted_overflow > 0 else min_power_w
        actual_w = min(actual_w, MAX_CHARGE_W)
        target_w = -actual_w  # negative = charge
        mode = "planned charge"

    else:
        # No plan for this slot — opportunistically charge from PV overflow
        if overflow_w - cars_w > 0:
            target_w = -min(overflow_w, MAX_CHARGE_W)  # negative = charge
            mode = "overflow charge"
        else:
            target_w = 0.0
            mode = "no action"

    plan_label = (
        f"+{planned_discharge_wh:.0f}Wh discharge" if planned_discharge_wh > 0
        else f"-{planned_charge_wh:.0f}Wh charge" if planned_charge_wh > 0
        else "none"
    )
    log.info(
        "Control [%s] target=%+.0fW  soc=%.0f%% (%.0fWh)  plan=%s  "
        "overflow=%.0fW  house=%.0fW  pv=%.0fW",
        mode, target_w, state.soc_pct or 0, soc_wh,
        plan_label, overflow_w, household_w, pv_w,
    )

    # Cache for web UI
    state_ref.last_zendure_state = state
    state_ref.last_home_power_state = hp
    state_ref.last_mode = mode
    state_ref.last_target_w = target_w

    # Refresh config-driven integrations and execute strategies
    if state_ref.registry is not None:
        await state_ref.registry.refresh_all()
        context = ControlContext(
            surplus_w=float(overflow_w),
            pv_power_w=float(pv_w),
            home_power_w=float(household_w),
        )
        await state_ref.registry.execute_strategies(context)

    if dry_run:
        log.info("  [DRY RUN — command not sent]")
        return

    await battery.set_power_w(target_w)

    # Track what we last set so the next tick can compute adjusted_overflow correctly
    if target_w < 0:
        state_ref._last_input_limit_w = -target_w
        state_ref._last_output_limit_w = 0.0
    elif target_w > 0:
        state_ref._last_input_limit_w = 0.0
        state_ref._last_output_limit_w = target_w
    else:
        state_ref._last_input_limit_w = 0.0
        state_ref._last_output_limit_w = 0.0


# ---------------------------------------------------------------------------
# Plan loop
# ---------------------------------------------------------------------------


async def _solve_plan(
    battery: ZendureIoBrokerDevice,
    sma_battery: SMAModbusIoBrokerDevice,
    tariff: TibberIoBrokerTariff,
    pv_forecast: PVForecastIoBrokerForecast,
    state_ref: "ControllerState",
) -> EnergyPlan:
    """Fetch state + forecasts, run MILP, return a fresh EnergyPlan."""
    battery_state = await battery.get_state()
    sma_state = await sma_battery.get_state()
    state_ref.last_sma_state = sma_state
    log.info("  Zendure: %.0f%%  SMA: %s",
             battery_state.soc_pct or 0,
             f"{sma_state.soc_pct:.0f}%" if sma_state.soc_pct else "n/a")

    horizon = timedelta(hours=HORIZON_H)
    pv_points = await pv_forecast.get_forecast(horizon)

    controllable = [
        d.storage_constraints for d in [battery, sma_battery]
        if d.storage_constraints is not None
    ]
    context = OptimizationContext(
        device_states={battery.device_id: battery_state, sma_battery.device_id: sma_state},
        storage_constraints=controllable,
        tariffs={"default": tariff},
        forecasts={ForecastQuantity.PV_GENERATION: pv_points},
        horizon=horizon,
    )
    optimizer = MILPOptimizer(
        baseline_load_kw=BASELINE_LOAD_KW,
        feed_in_price_eur_per_kwh=FEED_IN_EUR_PER_KWH,
        solver_msg=False,
    )
    plan = await optimizer.optimize(context)
    log.info("  Plan: %d actions over %d h", len(plan.actions), plan.horizon_hours)
    return plan


async def plan_loop(
    battery: ZendureIoBrokerDevice,
    sma_battery: SMAModbusIoBrokerDevice,
    tariff: TibberIoBrokerTariff,
    pv_forecast: PVForecastIoBrokerForecast,
    state_ref: ControllerState,
) -> None:
    """Re-solve the MILP every plan_interval_s and update state_ref.plan."""
    while True:
        log.info("Plan loop: solving MILP …")
        try:
            state_ref.plan = await _solve_plan(battery, sma_battery, tariff, pv_forecast, state_ref)
        except Exception:
            log.exception("plan_loop: solve failed — keeping previous plan")
        await asyncio.sleep(PLAN_INTERVAL_S)


# ---------------------------------------------------------------------------
# Control loop
# ---------------------------------------------------------------------------


async def control_loop(
    battery: ZendureIoBrokerDevice,
    home_power: HomePowerIoBrokerDevice,
    grid_meter: SMAEMIoBrokerDevice | None,
    tibber_live: TibberLivePowerIoBrokerDevice | None,
    mt175: MT175HADevice | None,
    state_ref: ControllerState,
    dry_run: bool,
) -> None:
    """Apply charge/discharge setpoint every control_interval_s seconds."""
    while True:
        try:
            await control_step(battery, home_power, grid_meter, tibber_live, mt175, state_ref, dry_run)
        except Exception:
            log.exception("control_loop: step failed — continuing")
        await asyncio.sleep(CONTROL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(dry_run: bool) -> None:
    log.info(
        "Starting energy controller  control=%ds  plan=%ds  dry_run=%s",
        CONTROL_INTERVAL_S, PLAN_INTERVAL_S, dry_run,
    )

    async with IoBrokerClient(HOST, PORT) as client:
        battery = ZendureIoBrokerDevice(
            device_id="zendure",
            client=client,
            hub_id=HUB_ID,
            device_serial=DEVICE_SERIAL,
            capacity_kwh=CAPACITY_KWH,
            max_charge_kw=MAX_CHARGE_KW,
            max_discharge_kw=MAX_DISCHARGE_KW,
            maintenance_charge_w=float(_cfg["zendure_iobroker"].get("maintenance_charge_w", 300.0)),
        )
        _sma_cfg = _cfg.get("sma_modbus_iobroker") or {}
        sma_battery = SMAModbusIoBrokerDevice(
            device_id="sma_battery",
            client=client,
            **{k: v for k, v in _sma_cfg.items() if k.startswith("oid_")},
        )
        tariff = TibberIoBrokerTariff(
            tariff_id="tibber",
            client=client,
            home_id=HOME_ID,
            include_tomorrow=True,
        )
        pv_forecast = PVForecastIoBrokerForecast(
            client=client,
            plant_id=PVFORECAST_PLANT_ID,
            tz=PVFORECAST_TZ,
        )
        home_power = HomePowerIoBrokerDevice(
            device_id="home_power",
            client=client,
            **{k: v for k, v in _hp_cfg.items() if k.startswith("oid_")},
        )
        grid_meter: SMAEMIoBrokerDevice | None = None
        if _em_cfg.get("serial"):
            grid_meter = SMAEMIoBrokerDevice(
                device_id="grid_meter",
                client=client,
                serial=str(_em_cfg["serial"]),
                **{k: v for k, v in _em_cfg.items() if k.startswith("oid_")},
            )
        tibber_live: TibberLivePowerIoBrokerDevice | None = None
        if HOME_ID:
            tibber_live = TibberLivePowerIoBrokerDevice(
                device_id="tibber_live",
                client=client,
                home_id=HOME_ID,
                **{k: v for k, v in _cfg.get("tibber_iobroker", {}).items() if k.startswith("oid_")},
            )

        mt175: MT175HADevice | None = None
        _ha_url = _ha_cfg.get("url")
        if _ha_url:
            _ha_token = _secrets.get("ha_token")
            ha_client = HAClient(url=_ha_url, token=_ha_token)
            mt175 = MT175HADevice(
                device_id="mt175",
                client=ha_client,
                **{k: v for k, v in _mt175_cfg.items() if k == "entity_id"},
            )

        # Read initial SoC to seed the hour-start snapshot
        battery_state = await battery.get_state()
        initial_soc_wh = (battery_state.soc_pct or 50.0) / 100.0 * CAPACITY_WH
        state_ref = ControllerState(initial_soc_wh)

        # Load config-driven integrations
        _ha_client_for_integrations = ha_client if _ha_url else None
        state_ref.registry = load_integrations(
            _cfg.get("integrations") or [],
            client,
            _ha_client_for_integrations,
        )

        # plan_loop and control_loop both start immediately.
        # control_loop will idle gracefully until the first plan is ready
        # (the plan_loop's first iteration typically completes in a few seconds).
        app = create_app(state_ref)
        uvi_config = uvicorn.Config(
            app, host="0.0.0.0", port=SERVER_PORT, log_level="warning"
        )
        uvi_server = uvicorn.Server(uvi_config)
        log.info("Web UI available at http://localhost:%d", SERVER_PORT)
        await asyncio.gather(
            plan_loop(battery, sma_battery, tariff, pv_forecast, state_ref),
            control_loop(battery, home_power, grid_meter, tibber_live, mt175, state_ref, dry_run),
            uvi_server.serve(),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Energy controller daemon")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute setpoints but do not send commands to the device",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))

# Control and safety

## Observe first, control per device

A battery, charger or thermal load that is added in the UI (also from a
preset) starts in **observe mode** (`control: observe` in its entry):
Nornwatt plans it like any other device and computes what it would
send, but sends it nothing — the device keeps its own logic.  Switch control
on per device on its page (Devices → the device → *Let Nornwatt
control this device*); this writes `control: active` into config.yaml.

* **Observe → control** takes effect at the next control tick (no reload).
* **Control → observe** releases the device at once (see the fail-safe
  below), so it goes back to its own logic.  An observed device is not
  released at start, reload or stop: its own settings are left alone.
* An entry **without `control:`** is controlled, so configurations from
  before observe mode keep working unchanged.
* The execution log (`/api/execution`, MCP `get_execution`) records what
  would have been sent, flagged `observed`.
* **Projected savings**: while a device is observed, a second savings
  accounting runs in which the observed devices follow the control loop
  (their power is the would-send setpoint, the grid changes by the same
  amount, an observed battery's SoC is simulated).  Its result — what control
  would have saved — is shown on the device page and, while every device is
  observed, on Home.  It is a simulation: the setpoints are computed on the
  real situation, and a car's or cooler's SoC/temperature is not simulated.
  `/api/savings` returns it under `projected`.
* `controller.dry_run` stays: it is the global switch for development and
  sends nothing to any device.

## When Nornwatt stops or loses control (fail-safe)

Nornwatt never leaves a device stuck in a commanded state: a battery
held at 0 W, a wallbox on "stop" or a cooler switched off.  Every device it
commands is **released**, back to its own normal operation:

| When | Released | Control again |
|---|---|---|
| Start, config reload, stop (add-on stop / SIGTERM) | at once | with the first plan |
| No current plan for `controller.plan_stale_after_min` (default 45 min, at least 3 planning intervals: one failed planning run is tolerated) | at once | with the next fresh plan |
| No control tick for `controller.control_stuck_after_min` (default 10 min; a watchdog independent of the control loop) | at once | with the next tick |

While released for a missing plan the control loop sends no commands.  Each
release and takeover is logged, and the UI shows a red notice while devices
are released.  Dry run sends nothing, releases included.

What "normal operation" is, per device:

| Device | Released to | Setting |
|---|---|---|
| SMA Sunny Boy Storage (`modbus`) | battery management default mode with the full charge / discharge limits: its own self-consumption | — |
| Zendure (`zendure`) | AC limits cleared, then its own mode (`autoModel`) from before Nornwatt first switched it to manual (remembered in the data folder) | `release_auto_model` to name the mode (e.g. 8 = smart matching) |
| openWB (`openwb_mqtt`) | the instant charging current and SoC limit Nornwatt changed are put back, then PV charging | `release_mode`: `pv` (default), `instant`, `stop`, `leave` |
| Switch (`homeassistant_switch`, e.g. the plug of a thermal load) | on: the load's own thermostat or timer takes over | `release_state`: `on` (default), `off`, `leave` |
| Thermal load (`homeassistant_thermal`) | through its switch | — |
| Thermostat (`homeassistant_thermostat`) | its `normal` target or mode (also after 30 min without a command) | `normal` |
| Battery / charger with mode control (`homeassistant_battery`, `homeassistant_charger`, `openwb_homeassistant`) | the `release` mode; default self-consumption (battery) or PV surplus (charger), if configured | `release` |
| Battery with `mode_power` control | the `release_option` | `release_option` |
| Battery / charger with other setpoint control (`number`, `charge_discharge`, `current`, `power`, `action`) | nothing: the last setpoint stays — use mode control where the device has modes | — |

**A hard crash** (power loss, out of memory, the container killed) cannot
release anything.  The next start releases every device first.  Until then:

| Device | After a hard crash |
|---|---|
| SMA | keeps the last battery-management command (verified to hold for minutes; no device-side timeout is known) until Nornwatt starts again |
| Zendure | stays in manual mode with the last AC limits (a forced charge continues until full) until Nornwatt starts again; switching back in the Zendure app also works |
| openWB | keeps the last charge mode and instant values; openWB resets the temporary charge template from the vehicle profile when the car is unplugged |
| Home Assistant switch, battery, charger, thermostat | keep the last state or mode.  Nornwatt's MQTT status (`nornwatt/status`) goes `offline` (last will) — a Home Assistant automation on it can switch the plug on or set the device's own mode |

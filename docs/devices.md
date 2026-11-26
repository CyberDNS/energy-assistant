# Devices

How to connect the devices of the house.

## Home Assistant devices

Any device Home Assistant can read or command is usable by configuration
only.  One type per category; each capability is **bound** to entities:

| Type | Category | Reads | Commands through `power_setpoint` |
|---|---|---|---|
| `homeassistant_sensor` | meter, pv, consumer, sensor | power, energy in/out, power per phase, temperature | — |
| `homeassistant_battery` | battery | `power_w` (+ = charging), `soc_pct` | `number` · `charge_discharge` · `mode_power` · `action` · `modes` |
| `homeassistant_charger` | charger | `power_w`, `plugged` | `current` (+ `enable` switch) · `power` · `action` · `modes` |
| `homeassistant_thermostat` | thermal_load | `temperature_c`, optional `power_w` | `target_temperature` · `hvac_mode` · `preset` · `water_heater` · `water_heater_operation` |
| `homeassistant_switch` | switch | optional `power_w`, `energy_in_kwh` | on / off |
| `homeassistant_thermal` | thermal_load | `temperature_c` | through the switch it is `controlled_by` |
| `homeassistant_vehicle` | vehicle | `soc_pct`, `range_km`, `plugged`, `location_home` | — |

(`generic_homeassistant` and `openwb_homeassistant` are older names that keep working.)

**Bindings** (every reading): `entity`, or `attribute` of it (e.g. a climate's
`current_temperature`), or `import_entity` + `export_entity` (value = import −
export; a battery's charge and discharge), or `entities` per phase; `invert_sign`, `scale`, and for on/off
values `on: [states that count as on]`.  Units come from the entity: kW, Wh,
°F, mi are converted.  The config UI only suggests entities of a fitting
domain, and for power and energy asks with a switch whether the value comes
from one signed entity or from one entity per direction, then shows only
those fields.  A missing entity is logged once and makes the value unavailable.
Home Assistant is read over one websocket: states are kept in memory, so a
control tick reads nothing over the network.

**Setpoint control or mode control.**  Every tick the control loop sends a
battery or charger a power *and* what it is for: `charge`, `discharge`,
`hold`, `absorb_surplus` (PV surplus only), `self_consumption` or `stop`.

- *Setpoint control* (`number`, `charge_discharge`, `current`, `power`,
  `action`, `mode_power`): Nornwatt decides the power every tick, also
  when following the PV surplus.  `mode_power` takes one `power` number for
  both directions, or `charge_power` + `discharge_power`.
- *Mode control* (`modes`): each behaviour maps to one of the device's own
  modes (`option` of the `mode` select), with an optional power or current
  number and fixed values (`set: {entity: value}`).  The device follows
  surplus and load itself — faster, fewer writes, and it keeps a sensible mode
  if Nornwatt stops (`release`).  A behaviour without a mode falls
  back to the closest one.

```yaml
- id: home_battery
  category: battery
  type: homeassistant_battery
  capabilities:
    power_w: { entity: sensor.battery_power }
    soc_pct: { entity: sensor.battery_soc }
    power_setpoint:
      control: modes
      mode: select.battery_working_mode
      modes:
        charge:           { option: Forcible charge, power: number.battery_forcible_power }
        discharge:        { option: Forcible discharge, power: number.battery_forcible_power }
        self_consumption: { option: Maximise self consumption }
        absorb_surplus:   { option: Maximise self consumption,
                            set: { number.battery_max_discharge_power: 0 } }
  params: { capacity_kwh: 10, max_charge_kw: 5, max_discharge_kw: 5 }
```

Writes go only on a mismatch (a value changed by hand is put back), at most
every 5 s per value; an entity that still shows another value a minute after
a write is logged once and reported as "not followed" in the device's state.
Chargers stop below `min_current_a` (6 A) and, following the surplus, keep
the minimum for `stop_delay_s` (60 s) so a passing cloud does not switch them
off.  Thermostats put back their `normal` target or mode when Nornwatt
stops and when no command came for 30 min; targets stay inside the
entity's own range.

**Presets.**  When you add a battery, charger or thermal load, the config UI
offers presets for common integrations: pick one, enter the entity prefix,
"Fill in the form", check and save.  Presets marked "not yet checked" are
written from the integration's documentation and source; please report what
you had to change.

| Preset | Category | Checked on a real installation |
|---|---|---|
| openWB chargepoint (openWB 2 integration) | charger | yes |
| Heater with a thermostat (HVAC mode / target temperature) | thermal_load | yes (generic thermostat) |
| Wallbox (core integration) | charger | not yet |
| go-e Charger (go-eCharger API v2) | charger | not yet |
| Huawei LUNA2000 (Huawei Solar) | battery | not yet |
| SolarEdge battery (SolarEdge Modbus Multi) | battery | not yet |
| Hot-water tank (water heater entity) | thermal_load | not yet |

New presets are a YAML file in `src/nornwatt/presets/` (format in
`core/presets.py`); the tests check every preset against the schemas.

## Thermal loads and switches

A load that keeps a measured value between two thresholds (aquarium cooler,
fridge, AC, water heater) is a `thermal_load` device: its `params` hold the
needs (`bottom_threshold`, `top_threshold`, `direction`, the fallback rates
`active_rate_per_h` / `drift_rate_per_h`, `min_runtime_h` / `min_offtime_h`),
and it reads its value from `capabilities.temperature_c` (`type:
homeassistant_thermal`).  The `switch` device it is `controlled_by` (a plug or
relay, `type: homeassistant_switch`) is the means: it holds `rated_power_w`,
reports the power (its optional `power_w` sensor, else the rated power while
on) and receives the on/off commands.  A thermostat (`climate` or `water_heater`
entity) switches itself: `type: homeassistant_thermostat` with its own
`params.rated_power_w` and no switch (see Home Assistant devices).  The
planner schedules the load in cheap
or PV hours; the control loop keeps it inside its thresholds whatever the plan
says.  Stored thermal models and savings are keyed by the thermal load's id.

## Vehicles and chargers

A car is a `vehicle` device read from its own Home Assistant integration
(`type: homeassistant_vehicle`, see `config.yaml.example`); a wallbox is a
`charger`.  When a charger reports a car plugged in, a **charging session**
starts with the charger's `default_vehicle`.  Each car has a **charger
selector** (UI: the car's card on Home and under Cars; Home Assistant: the car's
"Charger" select; API: `PUT /api/vehicles/<vehicle|guest>/charger?charger_id=<id|none>`).
The last selection wins: a car plugged in at that charger is reassigned now,
a free charger expects the selected car at its next plug-in, and a car whose
default charger is taken shows it greyed out until it is free again.  A
**guest** car charges with PV only or "Charge now" (UI: the guest card; Home
Assistant: "Guest car charger" / "Guest car mode"; API: `PUT
/api/guest/mode?mode=<pv_only|instant>`), by default the charger's
`guest_mode`.  The charger's plug state is authoritative; the car's own plug
sensor is only shown.

- **SoC:** the car's integration reports every 15–60 min.  In between, the SoC
  is estimated from the energy the charger delivered and the vehicle's charge
  curve; every new report resets the estimate.  The UI marks an estimated
  value ("est.") and shows the last report on hover.  `refresh_entity` +
  `params.refresh_after_min` press the integration's refresh button when the
  report gets old while charging.
- **openWB:** with `charger_ids: {openwb_mqtt: <openWB vehicle id>}` and
  `push_soc_to_charger: true` the SoC goes to openWB's MQTT SoC module
  (`set/mqtt/vehicle/<id>/get/soc`, range, timestamp).  A reassignment also
  sets openWB's vehicle for that chargepoint (`set/chargepoint/<n>/config/ev`;
  a guest is openWB's vehicle 0).
- **Needs on the car:** weekly plans, day overrides, force charge and "disable"
  belong to the vehicle; the charger only executes.  Its Home Assistant
  entities are published per vehicle id; entities of a vehicle that is no
  longer configured are removed from Home Assistant at the next start.
- **Savings** are still kept per charger (whichever car charged there).

## Tasmota meters and plugs

`type: tasmota` reads a Tasmota device straight over the LAN
(`GET http://<host>/cm?cmnd=Status%2010`), with no cloud and no MQTT broker. Use it
for IR reading heads on smart meters (Hichi, bitShake, … running an SML
script) and for plugs/relays with energy monitoring. Open the URL above in a
browser to see what your device reports.

The value names come from the device, so a **profile** maps them to readings:

| Profile | Device / script (Tasmota docs, Smart Meter Interface) |
|---|---|
| `energy` | Standard `ENERGY` sensor of plugs, relays, power monitors |
| `iskra_mt175` | Iskra MT175 script (`E_in`, `E_out`, `P`, `L1..L3`), also used in many adapted scripts |
| `iskra_mt631` | Iskra MT631 script (`Total_in`, `Total_out`, `Power_cur`) |
| `iskra_mt681` | Iskra MT681 script (as MT631, plus `Power_p1..p3`) |
| `easymeter_q3a` | EasyMeter Q3A / Apator APOX+ |
| `ebz_dd3` | eBZ DD3 (counters in Wh, converted) |
| `holley_dtz541` | Holley DTZ541 |
| `logarex_lk13be` | Logarex LK13BE (SML) |
| `dzg_dws7410` | DZG DWS7410 / DVS7420 |
| `landis_gyr_e320` | Landis+Gyr E320 |

Profiles describe a *script*, not the meter: pick the one whose value names
your device reports. `profile:` also takes a path to your own YAML file (same
format as `plugins/tasmota/profiles/*.yaml`). Only `energy` and `iskra_mt175`
are tested against real devices; the others follow the scripts in the docs.

```yaml
devices:
  # Main grid meter (Z1) via a Tasmota IR head
  - id: main_grid_meter
    category: meter
    type: tasmota
    tariff: grid
    host: 192.168.1.20
    profile: iskra_mt175
    # password: !secret tasmota_password   # web password, if set (user: admin)
    # poll_interval_s: 2                   # default: 2 for meters, 10 otherwise

  # The same meter mapped by hand: paths below StatusSNS
  - id: main_grid_meter
    category: meter
    type: tasmota
    host: 192.168.1.20
    fields:
      power_w: MT631.P                     # net, import +, export −
      energy_in_kwh: MT631.E_in
      energy_out_kwh: MT631.E_out
      phase_power_w: [MT631.L1, MT631.L2, MT631.L3]
    # invert_sign: true                    # if the script reports export as positive

  # Smart plug with standard Tasmota energy monitoring
  - id: dishwasher
    category: consumer
    type: tasmota
    host: 192.168.1.30
    profile: energy
```

- **Sensor name.** A profile's paths are relative to its sensor object (`MT175`,
  `ENERGY`). If your script names the meter differently, the one sensor object
  holding the power value is used. With several meters on one head, set
  `sensor: <name>` to pick one.
- **Overrides.** `fields` next to a profile replace the profile's, field by
  field, relative to the sensor; `null` removes one. A field is a path or
  `{path: …, scale: …}` (e.g. `scale: 1000` for kW). Quote paths that look like
  numbers (`"1_8_0"`). `voltage_v`, `current_a`, `phase_voltage_v`,
  `phase_current_a` and `extra: {name: path}` are optional and land in the
  device's `extra` (`l1_power_w` …, `meter_production_kwh` for `energy_out_kwh`).
- **Timestamps.** Readings are stamped with the receive time in UTC. The
  device's `Time` has no UTC offset and is ignored.
- **Robustness.** Timeouts, missing keys and the zeros an SML script reports
  right after a reboot are skipped, and the last good value is kept. The device
  only becomes unavailable after `stale_after_s` (default 30 s) without a valid
  reading.
- **Wrong mapping.** If the power value isn't found, the log names the bundled
  profiles that match the payload and the power-like values in it.

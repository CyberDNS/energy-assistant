# Domain Model

This document describes the conceptual model used to represent an energy
installation in the configuration. It defines the main top-level sections of
`config.yaml`, their purpose, and how they relate to each other.  The device
model (categories, capabilities, connections) is designed in issue #97.

---

## Overview

```
┌─────────────────────────────────────────────────────┐
│                     config.yaml                     │
├────────────┬──────────┬──────────┬────────┬─────────┤
│  backends  │ tariffs  │ devices  │ assets │optimizer│
└────────────┴──────────┴──────────┴────────┴─────────┘
```

| Section     | Purpose |
|-------------|---------|
| `backends`  | Connection parameters for ioBroker and Home Assistant |
| `tariffs`   | Pricing models defined once, referenced by name from devices |
| `devices`   | Everything in the house — meters, PV, batteries, chargers, consumers — with category, capabilities, params and connections; `metered_by` forms the metering tree |
| `assets`    | Threshold loads (until they become thermal_load + switch devices, #97) |
| `optimizer` | Single holistic solver: algorithm, horizon, schedule |

For a complete annotated example covering all sections, see [`config.yaml.example`](config.yaml.example).

---

## Layer 1 — `backends`

Connection parameters for the two supported data backends. These are
referenced implicitly by all device sources that use `type: generic_iobroker`
or `type: generic_ha`.

---

## Layer 2 — `tariffs`

Tariff models are defined here once and referenced by name from any device.
This keeps pricing configuration in one place.

---

## Layer 3 — `devices`

One flat list of everything in the house: meters, PV, batteries, chargers,
thermal loads, switches, consumers.  Parsed into `DeviceSpec`
(`core/device_model.py`) and validated at startup.

### Fields

| Field           | Required | Description |
|-----------------|----------|-------------|
| `id`            | yes      | Stable id (config key, DB key, MQTT key) |
| `category`      | yes      | What the device *is* (see categories below) |
| `type`          | yes      | The plugin — *how* to talk to the device; all keys not listed here are that plugin's settings (host, profile, …) |
| `label`         | no       | Display name |
| `tariff`        | no       | Tariff id from `tariffs:` (default: the default tariff) |
| `capabilities`  | no       | Bindings for capabilities the plugin doesn't provide itself, e.g. `power_w: {entity: sensor.…}` |
| `params`        | depends  | Model parameters of the category (battery capacity, grid limit, rated power) |
| `metered_by`    | no       | The meter this device sits behind |
| `controlled_by` | no       | The switch that executes this device's plan |
| `included_in`   | no       | A consumer whose meter already contains this consumer's load |
| `forecast`, `learning` | no | Consumption forecast (consumers) |

### Categories

| Category       | Required capabilities | Params | Connections |
|----------------|-----------------------|--------|-------------|
| `grid_meter`   | `power_w` | `max_import_kw`, `curtail_order`, `limit_hysteresis_kw` | — (root of the metering tree) |
| `meter`        | `power_w` | — | `metered_by` |
| `pv`           | `power_w` | — | `metered_by` |
| `battery`      | `power_w`, `soc_pct` | `capacity_kwh`, `max_charge_kw`, `max_discharge_kw` (required), SoC limits, efficiencies, wear cost, … | `metered_by` |
| `charger`      | `power_w`, `plugged` | `min_power_kw`, `max_power_kw`, `default_vehicle`, `guest_mode` | `metered_by`; the vehicle at it comes from the charging session |
| `thermal_load` | `temperature_c` | `rated_power_w` | `metered_by`, `controlled_by` → switch (or a `switch` capability binding for now) |
| `switch`       | `switch` | `rated_power_w` | `metered_by` |
| `consumer`     | — | — | `metered_by`, `controlled_by` → switch, `included_in` → consumer |
| `vehicle`      | `soc_pct` | `capacity_kwh` (required), `charge_limit_soc_pct`, `charge_curve`, `max_charge_kw`, `charger_ids`, `push_soc_to_charger`, … | dynamic: a charging session at a charger (see README "Vehicles and chargers") |
| `sensor`       | — | — | — |

Plugins declare which categories they can serve and which capabilities they
provide or can bind (`register_device(..., categories=, provides=, bindable=)`);
the loader checks every device against them before anything is built.

---

## Metering tree (`metered_by`)

`metered_by` describes the physical wiring of meters as a tree; the
`grid_meter` (the meter that sees the full imported/exported power) is the
root, children keep the order of the `devices:` list.  It is used for:

- **Cost attribution**: each `meter` below the root defines a tariff zone,
  owning the PV and batteries in its subtree.
- **Residual derivation**: a device whose power is not directly measured can
  be calculated as the parent meter minus all explicitly metered children
  (in practice: `type: differential` with explicit operands).
- **Power flow visualisation**.

Devices without `metered_by` (other than the grid meter) are known to the
system but are not part of the tree.  The tree must end at the grid meter:
a `metered_by` chain to a meter without its own `metered_by` is an error.

---

## Layer 5 — `assets`

Threshold loads (aquarium cooler, …) with their thresholds and thermal
model, referenced to their device by `device:`.  They become `thermal_load`
devices controlled by a `switch` in #97 phase 4.

EVs are no longer assets: a car is a `vehicle` device and its charging needs
(weekly plan, overrides, force charge) belong to it.  It enters the
optimization only while a charging session puts it at a charger.

---

## Layer 6 — `optimizer`

A single holistic optimizer runs over all controllable devices simultaneously.
This is the main value driver: joint optimization means the scheduler sees
all degrees of freedom and all constraints at once — battery arbitrage, EV
charging deadlines, heatpump pre-heating — and resolves them together.

The algorithm is a replaceable module. The default is MILP (Mixed Integer
Linear Programming via `pulp`). The same interface can be backed by a
rule-based scheduler, an ML model, or an LLM-driven planner in the future.

---

## Relationships between sections

```
tariffs:          ←── referenced by name from devices[*].tariff
devices:          ←── referenced by id from devices[*].metered_by /
                      controlled_by / included_in
                  ←── referenced by id from assets[*].device
                  ←── type:differential references two device ids
assets:           ←── optimizer discovers constraints from active assets
optimizer:        ←── reads all devices + active asset constraints at runtime
```

---

## `secrets.yaml`

Sensitive values (API tokens, passwords, home IDs) are stored in a separate
`secrets.yaml` file that is gitignored. They are referenced in `config.yaml`
with `!secret <key>`. See [`secrets.yaml.example`](secrets.yaml.example) for
the full list of supported keys.

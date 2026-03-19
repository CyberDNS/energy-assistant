# Domain Model

This document describes the conceptual model used to represent an energy
installation in the configuration. It defines the six top-level sections of
`config.yaml`, their purpose, and how they relate to each other.

---

## Overview

```
┌──────────────────────────────────────────────────────────────┐
│                          config.yaml                         │
├────────────┬──────────┬─────────┬────────┬──────────────────┤
│  backends  │ tariffs  │devices  │topology│ assets │ optimizer│
└────────────┴──────────┴─────────┴────────┴────────┴──────────┘
```

| Section     | Purpose |
|-------------|---------|
| `backends`  | Connection parameters for ioBroker and Home Assistant |
| `tariffs`   | Pricing models defined once, referenced by name from devices |
| `devices`   | Every piece of hardware — meters, consumers, producers, storage |
| `topology`  | The structural wiring of meters (tree, children, residuals) |
| `assets`    | Managed objects (EVs, heat stores) with their own targets |
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

Every hardware component is defined here — physical meters, consuming devices,
producing devices, and storage. This is the central registry of everything
the system knows about.

### Common fields

| Field      | Required | Description |
|------------|----------|-------------|
| `role`     | yes      | Semantic label — what this device *is* (see roles below) |
| `source`   | yes*     | How to read the current power value |
| `tariff`   | no       | Reference to a tariff name from `tariffs:` |
| `control`  | no       | How to actuate the device |

\* Not required for `role: consumer` devices that are pure residuals with no
direct sensor — they derive their power from `topology` position.

### Roles

| Role         | Meaning |
|--------------|---------|
| `meter`      | A physical energy meter. May be a grid connection point or a sub-meter. |
| `producer`   | A generation source (PV inverter, CHP, etc.) |
| `consumer`   | A load device (heatpump, appliance, circuit) |
| `storage`    | A battery or thermal store that can charge and discharge |
| `ev_charger` | An EV charging point — specialisation of consumer with asset association |

`tariff:` can be set on any role, not just `meter`. A consumer with a
special tariff (e.g. a heatpump on a heating tariff) declares it directly.

### Source types

| Type                 | Description |
|----------------------|-------------|
| `generic_iobroker`   | Reads a single power OID, or derives `import - export` from two OIDs |
| `generic_ha`         | Reads a single HA entity, or derives from import/export entities |
| `differential`       | Derives power as `minuend - subtrahend` (two device names) |

### Control types

| Type         | Description |
|--------------|-------------|
| `ha_switch`  | Calls `homeassistant.turn_on/off` on a HA entity |
| `iobroker`   | Writes a boolean OID in ioBroker |

---

## Layer 4 — `topology`

The topology describes the physical wiring of meters as a tree. It is purely
structural — device capabilities and sources are declared in `devices:`, not
here. The topology tree is used for:

- **Residual derivation**: a device whose power is not directly measured can
  be calculated as the parent meter minus all explicitly metered children.
- **Cost attribution**: energy costs are computed per sub-branch using the
  tariff declared on the corresponding device.
- **Power flow visualisation**: the dashboard renders the tree to show where
  energy is flowing.

The root of the tree is the grid connection point (the meter that sees the
full imported/exported power). Each node may have `children` that are meters
or devices with their own power readings.

Any device listed in `topology` must also be defined in `devices`. Devices
not listed in `topology` are still known to the system but are not part of
the structural tree.

### Residual derivation

If a device has no `source` of type `generic_*` but appears as a child in
the topology, its power is derived automatically: parent power minus the sum
of all directly-measured children at the same level. Only one un-measured
device may exist per parent node (otherwise the residual is ambiguous).

In the `devices:` example above, `heatpump` has `type: differential` and
names its operands explicitly (preferred). The topology residual mechanism
handles the case where no sensor exists at all.

---

## Layer 5 — `assets`

Assets are managed objects — things that store energy and have a target state.
Examples: an EV with a target SoC and a departure time, a hot water tank
with a target temperature and a time window for cheap heating.

An asset is always associated with the device that controls it via `managed_by`.

Asset constraints are discovered dynamically by the optimizer at runtime.
When an EV is not connected, no constraint is active. When it connects, its
`target_soc` / `target_by` constraint becomes part of the next optimization
run automatically.

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
devices:          ←── referenced by name from topology nodes
                  ←── referenced by name from assets[*].managed_by
                  ←── source.type:differential references two device names
topology:         ←── references device names as tree nodes
assets:           ←── optimizer discovers constraints from active assets
optimizer:        ←── reads all devices + active asset constraints at runtime
```

---

## `secrets.yaml`

Sensitive values (API tokens, passwords, home IDs) are stored in a separate
`secrets.yaml` file that is gitignored. They are referenced in `config.yaml`
with `!secret <key>`. See [`secrets.yaml.example`](secrets.yaml.example) for
the full list of supported keys.

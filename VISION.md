# Energy Assistant

## Vision

An open-source, vendor-neutral energy management platform that gives homeowners
full control over their energy infrastructure — without being locked into any
proprietary ecosystem, cloud service, or hardware brand.

Think of it as the **Home Assistant for energy management**: modular, extensible,
community-driven, and self-hostable.

---

## Problem Statement

Modern homes increasingly combine multiple energy systems — solar inverters,
battery storage, EV chargers, heat pumps, smart meters — yet each comes with
its own proprietary app, cloud dependency, and closed API. There is no open,
vendor-neutral layer that can orchestrate all of these systems together toward
a common goal: minimizing cost, maximizing self-consumption, or reducing carbon
footprint.

Commercial systems that do exist are typically locked to a single hardware brand,
a single country's grid regulations, or a mandatory cloud subscription. They
optimize in isolation — the EV charger does not know what the battery is doing,
and neither knows what the heat pump is paying for electricity.

Energy Assistant solves this by providing a single, open, self-hosted platform
that understands the full picture — devices, energy flows, metering topology,
tariff structures, and physical constraints — and optimizes across all of them
simultaneously.

---

## Goals

- **Vendor independence:** No lock-in to any hardware brand or cloud service
- **Self-hostable:** Runs entirely on local infrastructure (e.g. a home server,
  Raspberry Pi, or NAS)
- **Modular by design:** Every component — data source, optimizer, actuator,
  UI — is a replaceable module with a defined interface
- **Extensible:** New device types, tariff models, and optimization strategies
  can be added by the community without touching the core
- **Integrations-first:** Designed to work alongside existing ecosystems
  (Home Assistant, MQTT, REST APIs) rather than replace them
- **Accessible:** A user with no programming experience should be able to
  integrate their devices through configuration alone. A developer should
  be able to build arbitrarily sophisticated integrations through plugins.
- **Transparent:** Every decision the system makes is explainable — users can
  see not just what is happening, but why

---

## Architecture Principles

### Modularity First
Every capability in Energy Assistant is a module. Sources, consumers, optimizers,
forecasters, storage backends, and UI components all implement well-defined
interfaces and can be added, removed, or replaced independently. The core
platform provides orchestration and communication — nothing more.

### Plugin System
Device integrations and optimization strategies are distributed as plugins.
A plugin may represent a single device brand (e.g. a Fronius inverter, a
Wallbox EV charger) or a category of functionality (e.g. day-ahead price
forecasting). Plugins are discovered and loaded at runtime, with no changes
required to the core.

### A Spectrum of Integration Complexity
Not every integration requires a plugin. Energy Assistant supports a continuous
spectrum from pure configuration to full Python plugins:
```
Simple ◄────────────────────────────────────────► Complex
Config-driven                                  Python plugin
generic_ha/iobroker  →  + tariff + control  →  battery (MILP)
differential source  →  + asset model       →  EV charger (SOC planning)
read-only sensor     →  + topology node     →  SMA EM (firmware protocol)
```

A user who wants to integrate their heat pump as a read-only consumer needs
only a few lines of YAML. A developer building a full battery integration with
SOC tracking and MILP participation writes a Python plugin. Both are
first-class citizens.

### Event-Driven Core
The platform is built around an internal event bus. Devices publish state
updates (power readings, SoC values, tariff signals), and modules subscribe
to the events they need. This decouples producers from consumers and makes
the system reactive to real-time changes without polling.

### Local First, Cloud Optional
All features must work fully offline. Cloud connectivity — for weather
forecasts, dynamic tariff APIs, or remote access — is always an opt-in
integration, never a requirement. No data leaves the local network unless
the user explicitly configures it.

### Stable Internal APIs
All interfaces between modules are versioned and documented. A plugin written
for one version of Energy Assistant will continue to work across future
versions within the same major version. Breaking changes are communicated
clearly and managed through a deprecation process.

### Separation of Concerns
The platform distinguishes strictly between:
- **Data acquisition** — reading state from the real world
- **Forecasting** — predicting future states (prices, generation, consumption)
- **Optimization** — deciding what actions to take and when
- **Actuation** — sending commands to devices
- **Observation** — logging, dashboards, and alerting

These layers communicate through defined interfaces and are independently
replaceable.

### Full-Horizon Reasoning
The optimizer must always reason over the complete planning horizon and the
complete cost chain. Greedy or myopic control strategies — those that make
locally optimal decisions without regard for future consequences — are
explicitly out of scope for the core optimizer. This is the only way to
correctly handle arbitrage, opportunity cost, and cross-device interactions.

---

## Core Concepts

### Configuration Structure

An Energy Assistant configuration is divided into six top-level sections,
each with a distinct responsibility. See `DOMAIN_MODEL.md` for the full
reference.

| Section     | Purpose |
|-------------|------------------------------------------------------------------|
| `backends`  | Connection parameters for ioBroker and Home Assistant |
| `tariffs`   | Pricing models defined once, referenced by name |
| `devices`   | Every piece of hardware — meters, consumers, producers, storage |
| `topology`  | The physical wiring of meters as a tree |
| `assets`    | Managed objects (EVs, heat stores) with their own targets |
| `optimizer` | Single holistic solver: algorithm, horizon, schedule |

### Devices

A **Device** is the central building block. Every hardware component —
physical meter, consumption device, generation source, battery, EV charger —
is declared as a device. Each device declares three orthogonal concerns:

- **Source** — how to read the current power value. Always required for
  devices with a sensor. May be omitted for residual consumers derived
  from the topology tree.
- **Control** — how to actuate the device (switch on/off, set power).
  Optional — a device without control is read-only.
- **Tariff** — which pricing model applies to this device's energy flows.
  Optional — can be set on any role, not just meters.

This separation means a device can participate at any level: pure observation,
manual control, or full optimization. Each concern can be configured
independently and upgraded over time without touching the others.

See [`config.yaml.example`](config.yaml.example) for a complete annotated
example covering all device types and roles.

### Device Roles

Every device declares a **role** that describes what it fundamentally is
in the energy system. The role determines which properties and MILP
contributions are available.

| Role         | Description | Controllable | MILP participation |
|--------------|-------------|--------------|--------------------|
| `meter`      | Physical energy meter (grid connection or sub-meter) | No | Topology and cost attribution |
| `producer`   | Generates energy (PV inverter, CHP) | No | Upper bound from forecast |
| `storage`    | Stores and releases energy (battery) | Yes | SOC dynamics, arbitrage |
| `consumer`   | Consumes energy (heat pump, boiler, appliance) | Optional | Demand constraint |
| `ev_charger` | Consumer with SOC and charging deadline | Yes | SOC planning, deadline |

### Generic Connectors

Three connector types are first-class citizens in Energy Assistant, covering
the vast majority of home automation backends without requiring any custom
code:

| Connector          | Description |
|--------------------|-------------|
| `generic_ha`       | Reads sensors and controls entities via the Home Assistant REST API |
| `generic_iobroker` | Reads and writes datapoints via the ioBroker simple-api |
| `differential`     | Derives power as `minuend − subtrahend` from two named devices |

Any sensor or switch reachable through these backends can be integrated
without writing any code.

### Derived Device Power

Not every device has a dedicated sensor. Energy Assistant supports devices
whose power is derived from two other devices via `type: differential`.

A common real-world use case is deriving heat pump consumption from the
difference between two physical meters — a pattern required by the
Wärmepumpentarif metering concept common in Germany, Luxembourg, and Austria,
where the utility installs one meter for total import and a second for house
load only.

The heat pump consumption is never directly measured — it is derived as
`household_meter − measuring_switch`. No plugin, no code, no extra hardware.

### Energy Topology

The topology describes the physical wiring of meters as a tree.
Device capabilities and sources are declared in `devices:`, not here —
the topology is purely structural.

The tree is used for:
- **Power flow visualisation** — the dashboard renders where energy is flowing
- **Cost attribution** — costs are computed per sub-branch using the tariff
  on the device at that node
- **Residual derivation** — a device with no direct sensor and exactly one
  un-metered branch can have its power inferred as parent minus metered children

The single top-level key is the grid connection point (the meter that
sees the full imported/exported power). Every node references a device
defined in `devices:`.

### Assets

An **Asset** is a managed object that stores energy and has a quantified
target — an EV that must reach 80 % SoC by 07:00, or a hot water tank that
must reach 55 °C before the morning peak tariff window. Assets are distinct
from the devices that control them.

Asset constraints are discovered dynamically by the optimizer at runtime.
When an EV is not connected, no constraint is active. When it connects, its
target becomes part of the next optimization run automatically.

### The Optimizer

A single holistic optimizer runs over **all** controllable devices
simultaneously. This is the core value of the platform: joint optimization
means the solver sees every degree of freedom and every constraint at once —
battery arbitrage, EV charging deadlines, heatpump pre-heating — and
resolves them together. Splitting devices into separate plans defeats this
purpose.

The optimizer receives the current device states, the energy topology (for
cost attribution), all active asset constraints (discovered at runtime),
and all forecasts. It produces an **Energy Plan** — a time-indexed schedule
of control intents for all controllable devices over the planning horizon.

The algorithm is a replaceable module. The default is **Mixed-Integer Linear
Programming (MILP)**, which finds the globally optimal solution. The same
interface supports rule-based schedulers, ML models, or LLM-driven planners
without changing anything else in the system.

#### The Energy Plan

The output of the optimizer is a list of **intents with bounds** — not
fixed power setpoints. Each intent describes what a device should do in
a given timestep and within what limits, expressed in a way the control
loop can execute dynamically against real measured values.

For example, rather than instructing an EV charger to deliver exactly
3.2 kW, the plan assigns it `mode: pv_overflow, min: 1.4 kW, max: 11 kW`.
The control loop continuously resolves this against live PV and load
readings — the charger follows the actual surplus in real time.

Charge modes:

| Mode | Meaning | Control loop behavior |
|---|---|---|
| `pv_overflow` | Use surplus PV only | Track live overflow continuously |
| `grid_fill` | Draw from grid at planned power | Hold within bounds |
| `target_soc` | Reach SoC by deadline | Distribute remaining energy over remaining time |
| `idle` | Do nothing | No command sent |
| `discharge` | Feed stored energy into home | Track live deficit |

Modes are inferred from the MILP solution by a post-processing step in
each device plugin. The solver produces numbers; the plugin interprets
their meaning.

#### Transparency and Reporting

At every planning cycle the optimizer produces a cost comparison:

- **Baseline cost** — projected cost with no optimization
- **Optimized cost** — projected cost of the Energy Plan
- **Projected saving** — absolute and percentage, broken down per device
- **Actual vs. planned** — after the horizon closes, real cost vs. plan

This gives users visibility into what the system is doing, why, and how
much value it is delivering. Over time it also reveals forecast accuracy
and model quality.

### The Two Control Loops

Energy Assistant operates two complementary loops that bridge the gap
between forecasts and reality.

#### Planning Loop (slow)
- Runs every 15 minutes, or triggered on significant deviation
- Uses forecast data for prices, PV generation, and consumption
- Produces a full Energy Plan over the configured horizon
- Passes current device states, active asset constraints, and battery cost ledger as initial conditions
- Output: Energy Plan with intents and bounds per device per timestep

#### Control Loop (fast)
- Runs every few seconds
- Uses real measured values — actual PV, actual load, actual SoC
- Executes the current Energy Plan dynamically against live data
- Absorbs small deviations within plan boundaries without replanning
- Triggers a full replan when deviations exceed configured thresholds
- Maintains the battery cost ledger continuously
- Output: real-time device setpoints
```
Live sensor data
      │
      ▼
Control Loop  ◄──────────────────  Current Energy Plan
      │                                      ▲
      ├── setpoints to devices               │
      ├── updates battery cost ledger        │
      └── deviation detected ────────►  Replan Trigger
                                             │
                                        Planning Loop
```

### Battery Cost Tracking

The optimizer must know the true cost of energy currently stored in the
battery to make correct arbitrage decisions. Using stored energy to serve
a consumer only makes sense if the storage cost is lower than the
alternative supply cost. If the battery was charged from expensive grid
power, dispatching it to a cheap fixed-tariff consumer loses money.

The control loop maintains a **cost ledger**: a running weighted average
of the cost per kWh currently stored. Every charge action adds energy and
its source cost. Every discharge action consumes cost proportionally.
This ledger is passed to the planning loop at every replan as an initial
condition.

The ledger resets through three mechanisms:

- **Planning loop override** — at every replan, the MILP calculates the
  forward-looking opportunity cost of stored energy and the control loop
  adopts this as the new reference. This is the primary mechanism and
  ensures cost tracking is always forward-looking rather than historical.

- **Natural reset on SoC minimum** — when the battery reaches its minimum
  SoC the ledger empties and restarts clean on the next charge cycle.

- **Cycle-based reset** — when cumulative energy throughput since the last
  reset equals the battery capacity the ledger resets, handling batteries
  that rarely reach their minimum SoC.

---

## Future Integrations

- **Home Assistant** — as a companion integration, similar to Music Assistant
- **MQTT** — for real-time device communication
- **REST / WebSocket APIs** — for third-party dashboards and controllers
- **Dynamic tariff providers** — Tibber, aWATTar, Amber, Creos, and others
- **Weather providers** — for PV generation forecasting
- **Grid carbon intensity APIs** — for carbon-aware optimization

---

## Non-Goals (for now)

- Grid-scale or commercial energy management
- Replacing Home Assistant (complement, not compete)
- Any mandatory cloud connectivity
- Supporting non-residential grid connection types
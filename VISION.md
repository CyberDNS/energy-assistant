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

---

## Core Concepts

### Devices
A **Device** is any physical or virtual component that either produces,
consumes, or stores energy. Each device is represented by a plugin that
exposes a normalized state (e.g. current power, SoC, availability) and
optionally accepts control commands (e.g. set charge rate, switch on/off).

Devices are categorized by their role:

| Category | Examples |
|---|---|
| Source | PV inverter, grid connection, wind turbine |
| Storage | Battery system, thermal buffer tank |
| Consumer | Base load, EV charger, heat pump, washing machine |
| Meter | Smart meter, CT clamp, virtual meter |

### Energy Plan
An **Energy Plan** is the output of an optimization run: a time-indexed
schedule of control actions across all controllable devices for a given
horizon. Plans are recomputed periodically and whenever a significant
deviation from forecast is detected.

### Optimizer
An **Optimizer** is a pluggable module that receives the current system
state and forecasts, and produces an Energy Plan. The platform ships with
a default optimizer; users and developers can replace or extend it.
Optimization objectives — cost, self-sufficiency, carbon intensity — are
configurable.

### Forecast Provider
A **Forecast Provider** is a pluggable module that supplies predictions for
a given quantity over the planning horizon. Examples include:

- Electricity spot prices (e.g. from day-ahead market APIs)
- PV generation (e.g. weather-based solar irradiance models)
- Household consumption (e.g. learned from historical data)

Multiple providers for the same quantity can coexist; the optimizer selects
or blends them based on configured preference.

### Tariff Model
A **Tariff Model** describes the pricing structure of the grid connection.
It may be a flat rate, a time-of-use schedule, or a dynamic price feed
(e.g. Tibber, aWATTar). Tariff models are plugins and directly feed into
the optimizer.

### Constraint
A **Constraint** is a hard or soft rule that the optimizer must respect.
Examples:
- EV must reach 80% SoC by 07:00
- Battery SoC must not drop below 10%
- Heat pump may only run when grid price is below a threshold

Constraints are declared by device plugins or configured by the user.

---

## Future Integrations

- **Home Assistant** — as a companion integration, similar to Music Assistant
- **MQTT** — for real-time device communication
- **REST / WebSocket APIs** — for third-party dashboards and controllers
- **Dynamic tariff providers** — Tibber, aWATTar, Amber, and others

---

## Non-Goals (for now)

- Grid-scale or commercial energy management
- Replacing Home Assistant (complement, not compete)
- Any mandatory cloud connectivity
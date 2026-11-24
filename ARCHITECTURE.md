# Architecture

This document describes the technical architecture of Energy Manager: the
structure, key abstractions, design decisions, and the rationale behind them.
It is intended as a reference for contributors and as a guide for incremental
implementation.

---

## Language & Runtime

- **Python 3.14+** — async-native, strong ecosystem, low barrier for contributors
- **`asyncio`** throughout — all I/O (devices, storage, event dispatch) is async
- **`pydantic` v2** — all data models use Pydantic for validation, serialization,
  and schema documentation
- **`pytest` + `pytest-asyncio`** (`asyncio_mode = "auto"`) for all tests

---

## Repository Layout

Uses the [src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
as recommended by PyPA. This prevents accidental imports from the working
directory and makes broken installs detectable during development.

```
src/
  energy_manager/
    core/              # Abstractions, event bus, registry, config & storage protocols
      models.py        # Pure data models: DeviceState, EnergyPlan, Measurement, etc.
      device.py        # Device protocol
      device_model.py  # DeviceSpec, categories, capabilities, connections, schemas
      composition.py   # devices → optimizer / control inputs
      event.py         # Event dataclass + EventBus
      forecast.py      # ForecastProvider protocol
      tariff.py        # TariffModel protocol
      optimizer.py     # Optimizer protocol
      constraint.py    # Constraint base class
      registry.py      # DeviceRegistry
      config.py        # ConfigEntry model + ConfigManager protocol
      config_schema.py # schemas of the sections other than devices
      storage.py       # StorageBackend protocol
      topology.py      # TopologyNode tree built from metered_by
    config/            # Config file handling
      yaml.py          # YamlConfigLoader: config.yaml → AppConfig
      store.py         # DeviceConfigStore: edits device entries of config.yaml in place
    storage/           # Storage backend implementations
      sqlite.py        # SqliteStorageBackend (aiosqlite)
    plugins/           # Built-in device & service integrations
      generic_homeassistant/   # HA sensor as meter / PV / consumer
      homeassistant_vehicle/   # car from its HA integration (SoC, range, plug)
      homeassistant_switch/    # on/off switch in HA (executes a thermal load)
      homeassistant_thermal/   # thermal load read from HA, switched by its switch
      tasmota/           # Tasmota devices read on the LAN (profiles/*.yaml per meter script or plug)
      tibber/            # Tibber prices (tariff) and live stream (meter)
      tibber_local/      # Tibber Pulse Bridge read on the LAN (SML telegrams)
      openwb_mqtt/       # openWB 2.x chargepoint over MQTT
      zendure/           # Zendure battery (cloud or local MQTT)
      _modbus/           # shared Modbus TCP transport + register codec
      modbus/            # profile-driven Modbus device (profiles/*.yaml per vendor model)
      differential/      # power = minuend − subtrahend of two devices
      learned/           # learned consumption, thermal and efficiency models
      milp_highs/        # the MILP optimizer (HiGHS)
      mqtt_publisher.py  # Home Assistant MQTT discovery entities
      ...
    server/            # HTTP API, WebSocket, startup lifecycle
tests/
pyproject.toml
```

Plugins ship with the core package but are only loaded when the user
configures them. External community plugins register via `entry_points`
without requiring changes to this repository.

---

## Core Abstractions

### Typing approach: `Protocol` over `ABC`

All interfaces are defined as `typing.Protocol`. Plugins do not need to
inherit from a base class — they only need to structurally match the
interface. This reduces coupling and makes third-party contributions easier.

### Device

A `Device` represents any physical or virtual component that produces,
consumes, or stores energy.

```python
class Device(Protocol):
    @property
    def device_id(self) -> str: ...

    @property
    def category(self) -> DeviceCategory: ...

    async def get_state(self) -> DeviceState: ...

    async def send_command(self, command: DeviceCommand) -> None: ...
```

`DeviceCategory` (`core/device_model.py`) says what a device is:
`METER`, `PV`, `BATTERY`, `CHARGER`, `THERMAL_LOAD`, `SWITCH`, `CONSUMER`,
`VEHICLE`, `SENSOR`.  It comes from the `category:` of the device's config
entry (`DeviceSpec`); see DOMAIN_MODEL.md for the category schemas.

`DeviceState` is a Pydantic model holding normalized readings (power,
SoC, availability, etc.) with a timestamp.

### EventBus

The event bus is the nervous system of the platform. Devices publish state
updates; modules subscribe to the events they care about. This decouples
producers from consumers.

```python
class EventBus:
    def subscribe(self, event_type: type[T], handler: Callable[[T], Awaitable[None]]) -> None: ...
    async def publish(self, event: Event) -> None: ...
    async def flush(self) -> None:  # drains all pending handlers; used in tests
```

All communication between layers goes through the event bus — no direct
method calls between modules.

### ForecastProvider

Supplies predictions for a scalar quantity over a planning horizon.

```python
class ForecastProvider(Protocol):
    @property
    def quantity(self) -> ForecastQuantity: ...  # PRICE, PV_GENERATION, CONSUMPTION

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]: ...
```

Multiple providers for the same quantity can coexist; the optimizer selects
or blends them.

### TariffModel

Describes the pricing structure of the grid connection.

```python
class TariffModel(Protocol):
    async def price_at(self, dt: datetime) -> float: ...           # €/kWh
    async def price_schedule(self, horizon: timedelta) -> list[TariffPoint]: ...
```

Implementations can be static (flat rate, ToU schedule) or dynamic
(live feed from Tibber, aWATTar, etc.).

### Optimizer

Receives current system state and forecasts; returns an `EnergyPlan`.

```python
class Optimizer(Protocol):
    async def optimize(self, context: OptimizationContext) -> EnergyPlan: ...
```

`OptimizationContext` contains the current `DeviceState` for all registered
devices, the energy topology (for cost attribution), available forecasts,
all active tariff models, and all active asset constraints discovered at
runtime.

`EnergyPlan` is a time-indexed schedule of control actions (Pydantic model).

### Constraint

A hard or soft rule the optimizer must respect. Declared by device plugins
or configured by the user.

```python
@dataclass
class Constraint:
    device_id: str
    description: str
    is_hard: bool

    def is_satisfied(self, plan: EnergyPlan) -> bool: ...
```

---

## Configuration

### One file: `config.yaml`

Devices and the energy model are declared in `config.yaml`, which is the
single source of truth — whether it is edited by hand or from the web UI.
See [`DOMAIN_MODEL.md`](DOMAIN_MODEL.md) for the full reference and
[`config.yaml.example`](config.yaml.example) for a complete annotated example.

The main top-level sections are: `backends`, `tariffs`, `devices` (with
`metered_by` connections forming the metering tree), and `optimizer`.

### Editing from the UI

**Settings** covers the whole file and `secrets.yaml`, one page per topic
(`frontend/src/settings.ts`). Every form is
generated from the schemas the loader validates against, so the UI offers
exactly what the loader accepts:

| Part of the file | Schema |
|---|---|
| devices: capabilities, params, allowed connections | `CATEGORY_SCHEMAS` (`core/device_model.py`) |
| devices: plugin settings, bindable capabilities, binding keys | each plugin's `register_device(..., settings=…, binding=…)` |
| `tariffs:`, `forecasts:`, a device's `forecast:` | each plugin's `register_tariff` / `register_forecast(..., settings=…)` |
| `backends`, `optimizer`, `controller`, `server`, `logging`, `energy_dashboard` | `SECTION_SCHEMAS` (`core/config_schema.py`) |
| a device's `learning:` options | `LEARNING_OPTIONS` (`plugins/learned`), `THERMAL_LEARNING` (`core/config_schema.py`) |

All of them are built from `ParamSpec`: a kind (`float`, `int`, `bool`, `str`,
`list`, `dict`), limits and choices, and for structured values `items` (list
items), `fields` (a mapping with fixed keys) or `values` (a mapping with free
keys). Flags say what a value refers to (`ref` a device, `tariff`, `entity`,
`secret`, `plugin`), when it applies (`when`: only while sibling settings have
given values, which also limits `required`) and how the UI treats it
(`label`, `default`, `advanced`, `legacy`, `restart`). Plugins, categories and
capabilities have a `title`; a setting without a `label` gets one derived from
its key (`max_charge_kw` → "Max charge (kW)"). `loader/config_validation.py`
checks the sections and collections; `validate_plugins` checks the devices.

**Frontend components** (`frontend/src/config/`). The editors are reusable and
schema-driven; a form is a `SchemaForm` over a `{key: spec}` mapping.

| Component | Edits |
|---|---|
| `fields/FieldInput` | picks the editor for a spec (`editorFor`) |
| `fields/SchemaField`, `fields/SchemaForm` | a labelled field; a grid of fields for a mapping |
| `fields/ScalarInput`, `OptionSelect`, `EntityPicker` | single values, dropdowns, HA entities |
| `fields/SecretPicker` | a reference to a secret, with creating / replacing it in place |
| `fields/ListEditor`, `TableEditor`, `MapEditor` | lists, lists of records, free mappings |
| `fields/PluginEditor` | `type` + that plugin's settings (tariff, forecast, fallback) |
| `fields/LearningEditor`, `LoadProfileEditor`, `JsonEditor` | `learning:`, static profiles, raw fallback |
| `FormShell` + `useConfigForm` | the frame of a form: live validation, save, remove |
| `DeviceForm`, `CollectionPanel`, `SectionForm`, `SecretsPanel` | the pages, composed from the above |

A new kind of value needs one editor component and one line in `editorFor`; a
new setting only needs its `ParamSpec`.

A save (`PUT /api/config/…`, `server/config_api.py`) goes through these steps:

1. `config/store.py` applies the change to the file text in memory. Only the
   lines of that entry or section are rewritten, in the list style the file
   already uses; comments, `!secret` tags and all other lines stay byte for
   byte. The result is parsed again and must hold exactly what was asked for.
2. The new text is checked exactly as a start with it would be. Problems are
   returned as a list and nothing is written.
3. The file is written atomically; the previous version is kept in
   `config_backups/` next to it (the newest 20).
4. `Application.reload_config()` applies it without a restart.

**Secrets** never reach the UI. A `!secret name` value is exchanged as its
name; a plain value of a setting declared `secret` is exchanged as a
placeholder that means "keep what is in the file". `SecretsStore` sets and
removes entries of `secrets.yaml` (write-only values, mode 600); a secret
that is in use cannot be removed, and replacing one applies the
configuration again.

### Reload without a restart

`reload_config()` validates the file first (an invalid file changes nothing),
then stops the polling, planning and control loops, rebuilds everything that
is built from the configuration (`_build_runtime`: devices, tariffs,
forecasts, fleet, control loop, learners, MQTT publisher) and starts the
loops again. The API server and the database stay up; connections that the
new configuration still uses (Home Assistant client, Modbus, MQTT bridges,
Tibber) are kept, the others are closed. Stored data is keyed by device id
and is picked up again. It is the same sequence as a restart, so it has the
same effects: no control ticks for a few seconds, a fresh plan right after,
and in-memory control state (e.g. minimum run times) starts over.

`POST /api/config/reload` does the same for a file edited by hand; a notice
on every page offers it when the file on disk differs from what is running
(`GET /api/config/state`).
`server.port`, `server.api_token` and `server.trusted_hosts` still need a
restart.

### Configuration needed: a config mistake never stops the process

`Application.start()` never fails on the configuration. When the file has
problems (invalid YAML, schema errors, a missing secret, an unknown plugin)
or building it raises, the problems are kept in `config_problems` and the
runtime is built from `config/yaml.fallback_config()`: no devices, tariffs or
forecasts; `server`, `logging` and `backends.homeassistant` when they can be
read (the API is locked to the trusted hosts if the file names an
`api_token` it cannot read); no MQTT (the publisher would remove the
entities of the missing devices). Only the API and the history maintenance
run — no polling, planning or control — and the stored savings state is not
overwritten. `configuration_state()` (in `/api/status`, `/health`,
`/api/config/state` and `/api/config/file`) tells the UI.

In this state the config API saves a change that still leaves problems, as
long as it adds none (`PUT /api/config/text` saves any text), and applies the
file as soon as it is valid: `reload_config()` then builds the real runtime
and starts the loops. While a configuration runs, a change with problems is
refused as before.

**Home Assistant of the add-on.** When `SUPERVISOR_TOKEN` is set and no
`backends.homeassistant` is configured, the loader adds the Supervisor proxy
(`http://supervisor/core`, websocket `ws://supervisor/core/websocket`) as the
Home Assistant backend (`HomeAssistantConfig.supervisor`), so a new add-on
install needs no token.

---

## Storage

Runtime device state is always **in-memory**. The `StorageBackend` is only
for persisted history (graphs, analytics, state across restarts).

```python
class StorageBackend(Protocol):
    async def write(self, measurement: Measurement) -> None: ...
    async def query(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Measurement]: ...
```

The only planned implementation is `SqliteStorageBackend` using `aiosqlite`,
with an index on `(device_id, timestamp)`. No external database server is
ever required.

Configuration is **never** stored in SQLite — only time-series data.

---

## Separation of Concerns

| Layer | Responsibility | Communicates via |
|---|---|---|
| **Data acquisition** | Read state from real-world devices | Publishes `DeviceStateEvent` |
| **Forecasting** | Predict future prices, generation, consumption | Queried by optimizer |
| **Optimization** | Decide what actions to take and when | Produces `EnergyPlan` |
| **Actuation** | Send commands to devices | Subscribes to `PlanUpdatedEvent` |
| **Observation** | Logging, storage, dashboards | Subscribes to all events |

These layers communicate through the event bus and defined protocol interfaces.
No layer holds a direct reference to another layer's implementation.

---

## Testing

- **`pytest` + `pytest-asyncio`** with `asyncio_mode = "auto"` in `pyproject.toml`
- Tests use a real `EventBus` instance — no mocking of the bus itself
- `EventBus.flush()` drains all pending handlers synchronously within the
  current event loop iteration, giving tests full control over ordering
- Device plugins are tested with lightweight fakes that implement the
  `Device` protocol — no real hardware required
- All Pydantic models are tested for validation boundaries

---

## Build Order

| Step | What |
|---|---|
| 1 | `pyproject.toml` scaffold + project structure |
| 2 | Data models (`DeviceState`, `EnergyPlan`, `Measurement`, `ConfigEntry`, etc.) |
| 3 | `EventBus` + tests |
| 4 | `Device` protocol + `DeviceCategory` enum + `DeviceRegistry` + fake device for testing |
| 5 | `ConfigManager` protocol + `YamlConfigManager` (backends / tariffs / devices / topology / assets / optimizer) |
| 6 | `TopologyNode` tree + residual derivation |
| 7 | `StorageBackend` protocol + `SqliteStorageBackend` |
| 8 | `TariffModel` protocol + flat-rate implementation |
| 9 | `ForecastProvider` protocol + pass-through stub |
| 10 | `Optimizer` protocol + MILP implementation |
| 11 | `Asset` constraint model + runtime discovery |
| 12 | First generic device plugins (`generic_homeassistant`, `differential`) |
| 13 | First typed device plugin (e.g. Zendure battery with SOC tracking) |

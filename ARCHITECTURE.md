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
      device.py        # Device protocol + DeviceCategory enum
      event.py         # Event dataclass + EventBus
      forecast.py      # ForecastProvider protocol
      tariff.py        # TariffModel protocol
      optimizer.py     # Optimizer protocol
      constraint.py    # Constraint base class
      registry.py      # DeviceRegistry
      config.py        # ConfigEntry model + ConfigManager protocol
      storage.py       # StorageBackend protocol
    config/            # Config manager implementations
      yaml.py          # YamlConfigManager (phase 1)
      json.py          # JsonConfigManager (phase 2, UI-configured)
    storage/           # Storage backend implementations
      sqlite.py        # SqliteStorageBackend (aiosqlite)
    plugins/           # Built-in device & service integrations
      fronius/
      tibber/
      mqtt/
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

`DeviceCategory` is an enum: `SOURCE`, `STORAGE`, `CONSUMER`, `METER`.

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
devices, available forecasts, the active tariff model, and all active
constraints.

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

### Phase 1 — YAML

Devices and integrations are declared in a YAML config file read at startup.
Config is read-only at runtime; changes require a restart.

```yaml
devices:
  - id: solar_inverter
    plugin: energy_manager.plugins.fronius
    host: 192.168.1.10
  - id: ev_charger
    plugin: energy_manager.plugins.wallbox
    host: 192.168.1.20
```

### Phase 2 — JSON files

UI-configured setups persist config entries as JSON files in the config
directory (similar to Home Assistant's `.storage/` directory). No restart
required; entries are hot-reloaded.

### ConfigManager protocol

Both phases implement the same interface, so nothing else in the platform
needs to change when moving from YAML to JSON:

```python
class ConfigManager(Protocol):
    async def load_entries(self) -> list[ConfigEntry]: ...
    async def save_entry(self, entry: ConfigEntry) -> None: ...
    async def delete_entry(self, entry_id: str) -> None: ...
```

`ConfigEntry` is a Pydantic model with a stable `id`, `plugin` reference,
and a freeform `data` dict validated by the plugin itself.

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
| 4 | `Device` protocol + `DeviceRegistry` + fake device for testing |
| 5 | `ConfigManager` protocol + `YamlConfigManager` |
| 6 | `StorageBackend` protocol + `SqliteStorageBackend` |
| 7 | `TariffModel` protocol + flat-rate implementation |
| 8 | `ForecastProvider` protocol + pass-through stub |
| 9 | `Optimizer` protocol + rule-based default optimizer |
| 10 | First real device plugin (e.g. Fronius, MQTT) |

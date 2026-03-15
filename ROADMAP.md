# Roadmap

This document describes the planned development phases for Energy Assistant.
It is intentionally sequential: each phase builds on the previous one.
Phases are not time-boxed — quality and correctness take priority over speed.

---

## Phase 1 — Core Foundation

The internal skeleton: data models, event bus, protocols, config, and storage.
No UI, no real devices, no network I/O. Everything is tested with fakes.

- [ ] `pyproject.toml` scaffold + `src/` layout
- [ ] Data models: `DeviceState`, `EnergyPlan`, `Measurement`, `ConfigEntry`, `DeviceCommand`
- [ ] `EventBus` + `Event` dataclass + tests (including `flush()` for deterministic test ordering)
- [ ] `Device` protocol + `DeviceCategory` enum + `DeviceRegistry`
- [ ] `ConfigManager` protocol + `YamlConfigManager`
- [ ] `StorageBackend` protocol + `SqliteStorageBackend` (aiosqlite, index on `device_id, timestamp`)

---

## Phase 2 — Planning & Optimization Stubs

The optimization pipeline scaffolded end-to-end, but with simple implementations
that are good enough for initial real-world use.

- [ ] `TariffModel` protocol + flat-rate implementation
- [ ] `ForecastProvider` protocol + pass-through stub (returns zeros / flat line)
- [ ] `Optimizer` protocol + rule-based default optimizer
  - Minimize grid import when solar is available
  - Respect hard constraints (e.g. battery SoC limits, EV departure time)

---

## Phase 3 — First Real Integration: ioBroker

Rather than implementing device-specific drivers one by one, the first plugin
bridges Energy Assistant to [ioBroker](https://www.iobroker.net/) via its
`simple-api` HTTP adapter. This immediately covers dozens of inverter brands,
EV chargers, heat pumps, and smart meters that already have ioBroker adapters.

The plugin is config-driven: users declare object ID mappings in YAML.
No code changes are needed per device brand.

```yaml
devices:
  - id: solar_inverter
    plugin: energy_manager.plugins.iobroker
    host: 192.168.1.5
    port: 8087
    category: SOURCE
    state_map:
      power_w:    "fronius.0.inverters.0.Power"
      energy_kwh: "fronius.0.inverters.0.Energy_Day"
  - id: ev_charger
    plugin: energy_manager.plugins.iobroker
    host: 192.168.1.5
    port: 8087
    category: CONSUMER
    state_map:
      power_w: "go-e.0.chargers.abc123.nrg.11"
    command_map:
      set_power: "go-e.0.chargers.abc123.amp"
```

Deliverables:
- [ ] `IoBrokerDevice` implementing the `Device` protocol
- [ ] `simple-api` HTTP client (async, read state + write command)
- [ ] Config-driven state mapping (YAML → normalized `DeviceState`)
- [ ] Config-driven command mapping (`DeviceCommand` → ioBroker object ID write)
- [ ] Integration tests against a local ioBroker instance (opt-in, skipped in CI)

---

## Phase 4 — HTTP API & Basic UI

Make the system observable and operable without editing config files.

- [ ] FastAPI server with startup lifecycle (loads config, starts devices, starts optimizer loop)
- [ ] REST endpoints: current device states, active plan, history query
- [ ] WebSocket endpoint: live event stream for UI updates
- [ ] Minimal web dashboard: current power flows, active plan, device status

---

## Phase 5 — Dynamic Tariffs & Real Forecasts

Move from static assumptions to live market data.

- [ ] Tibber integration: live spot price + power readings via GraphQL subscription
- [ ] aWATTar integration: day-ahead EPEX spot prices
- [ ] PV generation forecast: integration with forecast.solar or Solcast
- [ ] Consumption forecast: simple rolling-average model

---

## Phase 6 — Direct Device Plugins

For users who don't run ioBroker, or who want tighter integration.

- [ ] Fronius Solar API (REST, no ioBroker required)
- [ ] MQTT generic device plugin (subscribe/publish topics, config-driven mapping)
- [ ] Home Assistant REST/WebSocket plugin (read entity states, call services)

---

## Phase 7 — Community & Ecosystem

- [ ] Plugin entry_points discovery (external community plugins, no core changes needed)
- [ ] Plugin developer guide
- [ ] Docker image + example `docker-compose.yml`
- [ ] Configuration UI (replaces YAML for non-technical users)

---

## Out of Scope (by design)

- Cloud-hosted SaaS version
- Mobile app (the web UI is responsive; PWA is sufficient)
- Direct integrations that duplicate ioBroker's existing adapter ecosystem

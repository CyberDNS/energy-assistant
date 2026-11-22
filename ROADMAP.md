# Roadmap

Working list for Energy Assistant: what exists, what is being worked on next,
and what comes later. Open items link to their GitHub issue.

## Direction

- **Native plugins where control or reliability matters:** batteries, grid
  meters, EV chargers, price sources. They run locally and need no cloud or
  bridge in the control path.
- **Home Assistant entities for everything else:** read-only consumers,
  sensors, simple switches. No extra plugin needed when HA already has the
  integration.
- **Learn from data instead of configuring profiles:** consumption and thermal
  models are fitted from HA long-term statistics and local history.
- **No ioBroker:** every source is native or comes from Home Assistant; the ioBroker plugins were removed (#112).
- **Accuracy before features:** plan quality and correct accounting (savings,
  ledger) come first, because every later feature builds on them.

---

## Done

### Core and planning
- [x] Event bus, device registry, metering topology (incl. Messkonzept 8 with differential meters)
- [x] Unified device model: `category` instead of `role`, category schemas with validation, `metered_by` instead of a separate `topology:` tree, composition step for the optimizer (#97 phase 2)
- [x] Consistency review: vehicle API (`/api/vehicles`), labels instead of ids in the UI, ledger only for batteries, legacy savings tables removed, docs and examples rebuilt (#97 phase 5, #112)
- [x] SQLite storage with retention; all timestamps stored in UTC (#62)
- [x] MILP optimizer (HiGHS) over the full horizon (up to 48 h, 15-min steps): batteries, EVs, threshold devices, secondary tariff zones
- [x] Battery ledger (cost basis of stored energy), degradation cost, standby losses, terminal value
- [x] Planning runs aligned to slot boundaries (#63)
- [x] Solver precision/time-limit knob; MIP gap and time-limit hits logged (`precision: 0.1` → 0 time-limit hits, median solve < 1 s)
- [x] Real-time control layer: follows the plan per slot, tracks slot energy for charge intents, shares PV by capability, grid connection limit (#75)
- [x] PV surplus at the household meter valued at the secondary tariff when separately billed loads would use it (#78)
- [x] Persisted plans, plan snapshots, control execution log and SoC corrections

### Devices and sources
- [x] Tibber: prices and cloud live meter (native)
- [x] Tibber Pulse read locally from the bridge, SML decoding (#61)
- [x] Tasmota: meters via IR heads (main grid meter) and energy plugs, read locally (#95)
- [x] Modbus TCP, profile-driven, with SMA Sunny Boy Storage profile (native)
- [x] Zendure via cloud or local MQTT (native)
- [x] openWB via MQTT or Home Assistant; vehicle assigned to the chargepoint by Energy Assistant
- [x] Generic Home Assistant devices, HA switches, HA threshold devices
- [x] Consumers imported automatically from the HA Energy dashboard ("individual devices"), incl. nesting (#93)
- [x] Forecast.Solar PV forecast (native)
- [x] Flat-rate and static-profile tariffs/forecasts
- [x] ioBroker-based plugins removed; a config using one fails at startup and names the replacement (#112)

### Vehicles, loads and learning
- [x] Vehicles as devices with their own HA data (SoC, range, plugged, location), independent of the charger (#97 phase 3)
- [x] Charging sessions: default vehicle per charger, reassignment in the UI/MQTT, guest car (PV only or charge now)
- [x] SoC estimated between car reports from charger energy × charge curve; optional SoC push to openWB
- [x] EV charging: weekly targets, day overrides, two-phase plan (cheap/PV first, instant charging before the deadline), range
- [x] Thermal loads (e.g. aquarium cooler) controlled by switch devices, with min run/off times; `assets:` removed (#97 phase 4, #109)
- [x] Learned consumption forecast from HA statistics (heat pump: temperature-driven); power sensors and energy meters as history
- [x] Learning on by default for every consumer with an HA power sensor; static profiles only as fallback
- [x] Learned household baseline as residual (household meter minus separately learned consumers) (#65)
- [x] Learned thermal model for threshold devices, optional ambient temperature (#88)
- [x] Battery round-trip efficiency learned from measurements (#58)

### Savings and accounting
- [x] Predicted (day-ahead) vs. real savings KPIs
- [x] Comparison against a simulated uncontrolled house, with a ledger on both sides (#82)
- [x] Battery standby, idle drift and forced recharge shared with the simulated house
- [x] Uncontrolled car recalculated after an early unplug
- [x] Threshold devices included; per-device savings panels (#89)

### UI, integration and operations
- [x] Built-in web UI: live state, plan, savings charts, per-vehicle plan views (embeddable)
- [x] MQTT to Home Assistant: EV entities, charging state, range, price curve, savings KPIs, non-HA devices as sub-devices
- [x] Read-only REST API and MCP endpoint with token
- [x] Docker image (GHCR), HA add-on (stable / dev / edge), release promotion model
- [x] Optional `api_url` for Tibber and Forecast.Solar to run against a local test server; isolated dev/test stacks per worktree

---

## Next: finish the device model (#97)

- [ ] Phase 6: configuration UI on top of the category schemas (device list by category, forms, "metered by" / "controlled by" / default-vehicle dropdowns)
- [ ] Changing a charger's default vehicle from the UI

## Next: vehicles

- [ ] Per-car minimum SoC and force charge as a forced load in planner and savings simulation (#99)
- [ ] Use the car's own plug sensor for chargers that cannot report a plug state; tune when a car charges right after plug-in
- [ ] Learn driving habits per car: daily energy use, departure/arrival, plug-in probability (#111)

## Next: plan quality

- [ ] Replay and backtest past days with different code or config (#68), incl. learned baseline vs. static profile
- [ ] Baseline shape scaled by day length / season (left out of #65)
- [ ] Multi-day price forecasts and a long-horizon (strategic) planning layer above the 48 h MILP (#110)
- [ ] MILP scalability: instrumentation, fewer binaries, warm start, coarser far horizon (#72); not urgent since `precision: 0.1`

## Then: operations and transparency

- [ ] Health checks, alerts and alert dashboard; alerts as MQTT sensors for HA (#67)
- [ ] Explain plan decisions per slot (#70)

## Later: features

- [ ] Phase-aware EV charging: fuse limit, 1/3-phase switching; first check the openWB interface (#69)
- [ ] Time-variable grid fees (§14a EnWG Modul 3), later §14a dimming (#71)
- [ ] Heat pump as controllable load via SG-Ready / HA entities (#66)

## Community readiness

- [ ] Issue forms for device requests and problem reports (#101)
- [ ] Support page: anonymised, encrypted support bundle with replayable scenario, pre-filled issue (#102)
- [ ] Tariffs: ENTSO-E day-ahead with fee/tax formula (#103), time-of-use (#104), power-dependent charges for Luxembourg and Flanders (#105)
- [ ] Generic devices: controllable batteries and wallboxes via HA entities (#106), OCPP 1.6J (#107), Modbus SunSpec (#108)

## Platform

- [ ] Plugin discovery via entry points
- [ ] Plugin developer guide
- [ ] Configuration UI (replaces YAML for non-technical users): see #97 phase 6

## Housekeeping (outside the repo)

- [ ] After a few quiet days without ioBroker: remove the stale HA leftovers (Zendure MQTT sensors and text entity, `tibber-adjusted-prices` sensor and its "Home Old" chart, the ioBroker restart automation), then remove the ioBroker container
- [ ] `push_soc_to_charger: false` for both cars (openWB reads the SoC from Home Assistant directly)

---

## Out of scope

- Cloud-hosted SaaS version
- Mobile app (the web UI is responsive; PWA is sufficient)
- Cloud services in the control path when a local interface exists

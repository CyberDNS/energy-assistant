# Roadmap

Where Energy Assistant stands and what comes next.  Work is tracked in
GitHub issues; this page is the overview.

---

## In place

- **Core:** device model with categories, capabilities, params and
  connections (`metered_by`, `controlled_by`, `included_in`), checked at
  startup (#97); event bus; SQLite storage; YAML config with secrets.
- **Planning:** one holistic MILP (HiGHS) over batteries, cars and thermal
  loads, on 15-minute slots, with dynamic prices and per-meter tariffs
  (Messkonzept 8); grid connection limit.
- **Forecasts:** PV from forecast.solar; learned consumption per consumer and
  a learned residual baseline (#65, #93); learned thermal models; learned
  battery round-trip efficiency (#94).
- **Devices:** Home Assistant sensors, Tasmota (IR heads, plugs), Tibber
  (API and Pulse Bridge), Modbus (profile-driven, SMA), Zendure, openWB 2.x
  over MQTT, HA switches.
- **Vehicles:** cars as devices with charging sessions at any charger, guest
  cars, SoC estimation between the car's reports, SoC push to openWB (#97).
- **Control:** control loop following the plan with safety overrides,
  execution log (plan vs. setpoint vs. measured).
- **Accounting:** savings against a simulated uncontrolled house.
- **Interfaces:** web UI (Live, Plan, History, Savings, Learning), REST API,
  read-only MCP endpoint, Home Assistant entities via MQTT discovery,
  Home Assistant add-on (dev / edge / prod channels).

---

## Next

- **Configuration UI** (#97 phase 6): device list by category, forms from the
  category schemas, "metered by" / "controlled by" / default-vehicle
  dropdowns; replaces editing `config.yaml` for most changes.
- **Vehicles:** use the car's own plug sensor for chargers that cannot report
  a plug state; tune when a car charges right after plug-in.
- **More devices:** e.g. heat pump / hot water as thermal loads, more Modbus
  profiles, more chargers.

---

## Out of scope (by design)

- Cloud-hosted SaaS version
- Mobile app (the web UI is responsive; PWA is sufficient)

# Configuration

Everything Nornwatt knows about the house is in `config.yaml` (and `secrets.yaml`).

## Devices: categories, capabilities, connections

Everything in the house is one entry of the flat `devices:` list:

```yaml
devices:
  - id: main_grid_meter
    category: meter               # what it is (no metered_by: the grid connection)
    type: tasmota                 # how to talk to it (the plugin) …
    host: 192.168.1.20            # … and the plugin's own settings
    profile: iskra_mt175
    tariff: grid
    params: { max_import_kw: 24 } # optional grid connection limit

  - id: pv_production
    category: pv
    type: homeassistant_sensor
    metered_by: main_grid_meter   # the meter it sits behind
    capabilities:                 # bindings for HA-backed devices
      power_w: { entity: sensor.power_production_pv, invert_sign: true }

  - id: home_battery
    category: battery
    type: modbus
    metered_by: main_grid_meter
    profile: sma_sunny_boy_storage
    host: 192.168.1.22
    params: { capacity_kwh: 7.4, max_charge_kw: 3.0, max_discharge_kw: 3.0 }
```

- **Categories:** `meter` (the one meter without `metered_by` is the grid
  connection, the others are sub-meters), `pv`, `battery`, `charger`,
  `thermal_load`, `switch`, `consumer`, `vehicle`, `sensor`. Each category's
  schema (`core/device_model.py`) lists the capabilities it needs or may bind
  (`power_w`, `soc_pct`, `temperature_c`, `switch`, …), its `params` and the
  connections it may have.
- **Connections:** `metered_by` builds the metering tree (its root is the meter
  at the grid connection, which also carries the grid limit `max_import_kw`), `controlled_by` names the
  switch executing a device's plan, `included_in` marks a consumer whose load
  is inside another consumer's meter.
- **Checked at startup:** unknown categories, params or capabilities, missing
  required ones, plugins that can't serve a category, unknown or ill-typed
  plugin settings and broken `metered_by` chains stop the start with a list of
  everything to fix.

## Configuring in the UI

**Settings** edits everything in `config.yaml` and `secrets.yaml`, one page per topic:

| Page | What |
|---|---|
| Devices | the `devices:` list, grouped by category |
| Tariffs | `tariffs:` |
| Forecasts | `forecasts:` (system-wide, e.g. PV) |
| Connections | `backends:` (Home Assistant, MQTT broker) |
| System | `optimizer:`, `controller:`, `energy_dashboard:`, `server:`, `logging:` |
| Secrets | the entries of `secrets.yaml` |
| config.yaml | the whole file as text (also when it is not valid YAML) |
| User interface | theme and Home Assistant full-screen mode; stored in the browser, not in `config.yaml` |

Every form is built from the same schemas the loader checks the file against,
with an editor per kind of value: dropdowns for devices, tariffs and choices,
a searchable Home Assistant entity selector (by friendly name, entity id and
area, with the current state; only the fitting domains), suggestions from
the chosen entity for its options, modes and attributes, tables for charge curves, PV planes and
exclusion rules, a load-profile editor per day type, and nested forms for a
forecast and its fallback.

Fields carry a readable name; the `config.yaml` key is shown small in the hint
below. Dropdowns offer the real values with the default marked "(default)";
choosing the default leaves the key out of the file. Settings that only matter
for another choice appear with that choice (a Zendure cloud key only for the
cloud connection), and rarely needed ones are under "More settings".

- **Save and apply** checks the change with the same rules as the start,
  writes it to `config.yaml` and applies it right away, without a restart.
  Problems are listed in the form and nothing is written.
- Only the lines of the edited entry or section change in `config.yaml`; your
  comments and the rest of the file stay as they are. The previous file is
  kept in `config_backups/` next to it (the newest 20).
- **Secrets:** a password or token field holds the *name* of a secret. Pick an
  existing one or create one right in the field; the value goes to
  `secrets.yaml` (mode 600) and is never shown again. The Secrets page lists
  where each secret is used and lets you replace or remove values. Replacing a
  secret that is in use applies the configuration again.
- A device, tariff or forecast id cannot be changed afterwards. Removing a
  device keeps its history in the database.
- Devices imported from the Home Assistant Energy dashboard are listed
  read-only.
- `server.port`, `server.api_token` and `server.trusted_hosts` are marked
  "needs a restart"; everything else applies live. Applying takes a few
  seconds: control pauses, then a fresh plan is made.
- If you edit `config.yaml` by hand, the tab offers **Apply now** (or
  `POST /api/config/reload`) instead of restarting the add-on.
- The whole file is checked at startup too: an unknown section, an unknown or
  ill-typed setting of a section, tariff, forecast or plugin is listed with
  everything else to fix, and the app starts in *configuration needed* state
  (see [First start](installation.md#first-start)).

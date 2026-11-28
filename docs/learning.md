# Learning

Nornwatt learns forecasts and device models from the measured history.

The Learning tab groups the models by what is learned, and each group has its own
method: **Consumption** (daily energy per consumer from temperature and kind of
day, times an hourly shape), **Min/Max devices** (thermal model of devices kept
between two thresholds) and **Batteries** (round-trip efficiency). Each entry in
`GET /api/learned` carries its `category` (`consumption`, `thermal`, `storage`).

## Learned consumption forecasts

Consumers are forecast from Home Assistant's long-term statistics (`type:
learned`, see `plugins/learned/__init__.py`): a daily energy per weekday/weekend
(or, with `temperature_entity`, a function of the outdoor temperature) times a
learned hourly shape, refitted every 6 h.

- **Learning is on by default** for every consumer that reads one HA power
  sensor (`type: generic_homeassistant`): it learns its forecast from that
  sensor. A `static_profile` it declares is used until the first fit. Set
  `learning: false` to turn it off, or `learning: {history_days: 365, …}` to
  pass options.
- **Residual household baseline.** Instead of a static profile, the household
  baseline learns the household meter minus every consumer that has its own
  forecast, so nothing is counted twice:

  ```yaml
  - id: baseline
    category: consumer
    type: generic_consumer
    forecast:
      type: learned
      history_entity: sensor.power_consumption_household
      subtract: [auto]      # every learned consumer on the same tariff
      fallback: { type: static_profile, profile: … }
  ```

  `auto` follows the configuration: adding or removing a consumer changes the
  residual at the next refit. Loads the optimizer plans itself (EV chargers,
  batteries, threshold devices) are part of the plan, not of the forecast: if
  the household sensor contains one, add its power sensor to `subtract`
  (e.g. an aquarium cooler's smart plug), and exclude the dashboard entry
  containing it from the import.
- **Import the Energy dashboard.** With `energy_dashboard: {import_consumers:
  true}` every "individual device" of HA's Energy dashboard becomes a consumer
  with a learned forecast (live power from its power sensor, if it has one).
  Entries already used by a configured device are skipped; list EV chargers and
  other planned loads under `exclude:`. See `config.yaml.example`.

The Learning tab (Consumption) shows each model, its training days and fit quality.

## Learned thermal models

Threshold devices (aquarium cooler, fridge, AC, …) are planned with a model of
how their measured value moves: `rate = offset + a · (ambient − value) + gain ·
running`, with the ambient term only when an ambient sensor is configured.
Nornwatt learns it from Home Assistant's state history of the value
sensor and the device switch (refitted every 6 h, last 10 days) and uses it in
the planner; until a fit succeeds it uses the configured rates. The Learning tab
shows the learned rates next to the configured ones, the fit quality and the
measured vs. modelled value. Configuration: the thermal load's `learning:`
(`false` keeps the configured rates), see the aquarium example in
`config.yaml.example`.

## Learned battery efficiency and self-consumption

A battery loses energy in two ways: a share of every kWh it moves
(`charge_efficiency` / `discharge_efficiency`) and a constant
`self_consumption_w` (battery management, inverter) drawn from the stored energy
at all times. Both are learned from the measured AC power and SoC
(`core/battery_efficiency.py`) with an **energy balance**: windows of at least
12 hours from the battery resting to resting again. In each window the energy
charged and discharged must explain the change of the stored energy (from the
two SoC readings):

    η × in − out ÷ η − self-consumption × hours = change of the stored energy

A least-squares fit over all windows finds η (per direction) and the
self-consumption. One SoC reading's error does not add up over many windows,
while the losses grow with throughput and time. Windows with an unexplained SoC
jump (BMS re-calibration) are dropped, except from full to full; charging power
at or below `standby_w` is the battery's standby (household load), while
discharging counts down to 10 W.

Power history without a state of charge (an import without `soc`) still counts:
in windows of a month, where the unknown change of the stored energy (at most the
capacity) is small against the throughput. Each window weighs by its uncertainty;
when a battery's rest-to-rest windows do not fit (its SoC does not track its
energy), the estimate rests on the month windows alone.

The estimate is confident with 6 windows over 4 days that charged 1.5 × the
capacity, and when the model explains the windows within 3 % of the capacity —
a battery whose SoC reading does not track its energy keeps its configured
values.

```yaml
- id: zendure
  category: battery
  type: zendure
  params:
    capacity_kwh: 7.6
    max_charge_kw: 1.2
    max_discharge_kw: 0.8
    charge_efficiency: 0.95     # used until the estimate is confident
    discharge_efficiency: 0.95
    self_consumption_w: 0
    efficiency: learned         # default: configured
    efficiency_learning:        # optional
      history_days: 90
      refit_interval_h: 6
```

Every battery is learned, also with `efficiency: configured`, so you can compare
the values on the Learning tab (Batteries) or via `GET /api/learned` and
`GET /api/config` before switching. With `efficiency: learned` the plan and the
savings accounting's uncontrolled house use the learned values (a configured
charge/discharge asymmetry is kept): the real house's losses are what the SoC
readings show, so the uncontrolled house must run on the same battery.


## Past data from Home Assistant

A device can name Home Assistant entities with its past data, so learning starts
with data instead of from zero (`core/history_import.py`):

```yaml
- id: zendure
  category: battery
  history:                      # optional: past data, hourly
    power: { entity: sensor.zendure_power }              # one signed entity, or …
    soc: { entity: sensor.zendure_state_of_charge }      # batteries and cars only

- id: sma_battery
  category: battery
  history:
    power: { import_entity: sensor.sbs_charge_power,     # … one per direction, both ≥ 0
             export_entity: sensor.sbs_discharge_power }
    soc: { entity: sensor.sbs_soc }
```

The forms are those of a capability binding; the device settings offer the same
"Read from" switch. Choose entities whose history goes back before Nornwatt:
the entities Nornwatt publishes to Home Assistant itself only start when Nornwatt
did, so there is nothing to import from them ("Nothing to import").

Their long-term statistics (hourly, kept for good by Home Assistant) are imported
once, up to 2 years back, into Nornwatt's own hourly history. Learners and charts
read only that history. A measured hour always wins: the import never replaces
one, and a measured hour replaces an imported one. In the device settings
("Past data from Home Assistant") the status shows what was imported; the
imported hours can be removed again.

Nothing is imported unchecked. Over the hours both have, the entity's power
must follow what Nornwatt measured (correlation ≥ 0.9, scale 0.8–1.25): a
slope near −1 is imported with the sign inverted, anything else is refused with
the figures, and the state of charge must match within 5 points. Until Nornwatt
has measured the device for 48 hours the import waits — except for a battery,
whose state of charge must then rise while the power says charging. With one signed entity, hours that charged and discharged cannot be split from
hourly statistics and are left out; with one entity per direction every hour counts.
Hourly rows take about 0.9 MB per device and year, so the import stays small.

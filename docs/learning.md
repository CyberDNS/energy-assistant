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

## Learned battery efficiency

Each battery's effective round-trip efficiency is learned from the measured AC
power and SoC (`core/battery_efficiency.py`). SoC readings are coarse and a BMS
re-calibrates them, so it only uses **closed cycles**: the battery rests,
charges and discharges (at least 15 % of its capacity each way), and rests
again at the same SoC reading. The stored energy is then the same at both ends,
and energy out ÷ energy in is the round trip. Cycles with an unexplained SoC
jump are dropped, and power at or below `standby_w` is left out (standby draw
is household load, not a storage loss). The estimate is Σ out ÷ Σ in over the
cycles of the last 14 days, split evenly: η_charge = η_discharge = √round trip.

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
    efficiency: learned         # default: configured
    efficiency_learning:        # optional
      history_days: 14
      min_cycles: 3             # and 1.5 × capacity charged
```

Every battery is learned, also with `efficiency: configured`, so you can compare
the values on the Learning tab (Batteries) or via `GET /api/learned` and
`GET /api/config` before switching. Only the plan uses the learned value; the
savings accounting keeps the configured efficiencies, so a battery that performs
worse than configured shows up as lower savings instead of being hidden.

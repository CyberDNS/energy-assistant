# Nornwatt in Home Assistant

With `backends.mqtt` configured, Nornwatt publishes its own devices and entities to Home Assistant through MQTT discovery.

## Savings KPIs in Home Assistant

With `backends.mqtt` configured, a "Nornwatt" device exposes the
cumulative **Real savings** in € (`device_class: monetary`, `state_class:
total`). Use Home Assistant's `utility_meter` or statistics cards to get daily,
monthly or yearly figures.

The saving compares the real house with a **simulated uncontrolled house** on
the same measured load, PV and prices, per 15-minute slot:

- its batteries charge from PV surplus only and discharge to cover the load of
  the zone they regulate on (household and cars, not a separately billed heat
  pump),
- a car charges from PV surplus left after the batteries, and from the grid as
  late as possible to reach its charge plan's target by the deadline.

Both houses keep a ledger per battery: charging moves the energy's cost into
the ledger (grid energy at the import price, PV at the feed-in price it would
have earned) and using it costs the ledger's average cost. So a charging slot
saves nothing and the saving shows when stored energy replaces grid power:
(price at that moment − ledger cost) × kWh. Car energy charged earlier than
the uncontrolled car would have is held at its cost until the uncontrolled car
charges it too. Threshold devices (aquarium cooler, …) run in the uncontrolled house
as a plain thermostat between their thresholds, on the device's learned thermal
model; their energy is matched the same way, and the simulated value is set back
to the measured one every 24 h. A car unplugged before its plan's deadline gets the uncontrolled
car recalculated backwards from the unplug moment: it charges the same energy as
late as possible before unplugging, priced at those slots' measured prices. The Savings tab and `GET /api/savings?hours=168` show the
same numbers; for the 24 h and 3 d ranges the tab also charts, per 15-minute
slot, the stored energy and battery/car power of both houses next to the
price. Below the totals the tab lists one panel per controlled group, built
from the configuration: the batteries together, each car and each threshold
device, each with its own share of the saving. Details in
`src/nornwatt/core/savings.py`.

Threshold devices count as fixed load and contribute no saving. Nothing is
published in dry-run mode.

## Price curve in Home Assistant

With `backends.mqtt` configured, the "Nornwatt" device also exposes:

| Sensor | Meaning |
|---|---|
| `sensor.nornwatt_electricity_price` | Current import price (€/kWh); its attributes hold the whole curve |
| `sensor.nornwatt_effective_electricity_price` | What one extra kWh costs right now, given the PV and consumption forecasts |

The effective price is the import price while the site imports, the feed-in
price while PV surplus is forecast (using the energy loses that income), and
a blend of both in between (`share = (load − pv) / load`). It is a display
figure; the optimizer plans against the raw prices. Details:
`src/nornwatt/core/price_curve.py`.

The curve attribute `data` has one row per slot,
`[epoch_ms, price, effective, feed_in, estimated]`, from the start of the
last 24 hours to the end of the plan (refreshed after every plan).
`estimated = 1` marks prices the tariff has not published yet (repeated from
the previous day). An [ApexCharts card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Electricity prices
graph_span: 2d
span:
  start: day
now:
  show: true
  label: now
apex_config:
  legend:
    show: true
  yaxis:
    - decimalsInFloat: 3
series:
  - entity: sensor.nornwatt_electricity_price
    name: Import
    color: '#e07070'
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[4] ? null : r[1]]);
  - entity: sensor.nornwatt_electricity_price
    name: Import (estimated)
    color: '#e07070'
    stroke_dash: 4
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[4] ? r[1] : null]);
  - entity: sensor.nornwatt_electricity_price
    name: Effective
    color: orange
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[2]]);
  - entity: sensor.nornwatt_electricity_price
    name: Feed-in
    color: '#4caf7d'
    stroke_dash: 6
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[3]]);
```

## Weekly EV plan in Home Assistant

With `backends.mqtt` configured, each EV's weekly plan is exposed through MQTT
discovery as configuration entities on the EV device — per weekday a `number`
(target SoC), a `time` (departure, local time) and a `switch` (day enabled).
Changes are written straight to the weekly plan and trigger a re-plan; edits
made in the Nornwatt UI are mirrored back to the entities.

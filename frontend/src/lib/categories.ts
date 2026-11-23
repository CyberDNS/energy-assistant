// Device categories (core/device_model.py) as the UI names them.
export const CATEGORY_ORDER = ['pv', 'battery', 'vehicle', 'charger', 'thermal_load', 'switch', 'consumer', 'meter', 'sensor']

export const CATEGORY_TITLES: Record<string, string> = {
  pv: 'Solar',
  battery: 'Battery',
  vehicle: 'Car',
  charger: 'Charger',
  thermal_load: 'Heating or cooling load',
  switch: 'Switch',
  consumer: 'Consumer',
  meter: 'Meter',
  sensor: 'Sensor',
}

export const CATEGORY_GROUP_TITLES: Record<string, string> = {
  pv: 'Solar',
  battery: 'Batteries',
  vehicle: 'Cars',
  charger: 'Chargers',
  thermal_load: 'Heating and cooling',
  switch: 'Switches',
  consumer: 'Consumers',
  meter: 'Meters',
  sensor: 'Sensors',
}

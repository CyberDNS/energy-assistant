// Where the pages of devices live.  Cars have their own page (it is also what
// dashboards embed); every other device uses the generic device page.

/** The page of a device, by its category. */
export function deviceLink(device: { device_id: string, category?: string | null }): string {
  const id = encodeURIComponent(device.device_id)
  return device.category === 'vehicle' ? `/cars/${id}` : `/devices/${id}`
}

/** The sidebar item a device belongs to. */
export function deviceNav(category: string | null | undefined): string {
  switch (category) {
    case 'vehicle':
    case 'charger':
      return 'cars'
    case 'battery':
      return 'batteries'
    case 'thermal_load':
    case 'switch':
      return 'loads'
    default:
      return 'devices'
  }
}

/** The page behind a savings group: the batteries together (also their wear), a car, a device. */
export function savingsLink(group: { id: string, kind: string }): string {
  if (group.kind === 'batteries' || group.kind === 'wear') return '/batteries'
  const id = encodeURIComponent(group.id)
  return group.kind === 'car' ? `/cars/${id}` : `/devices/${id}`
}

/** The device a learned model belongs to (`<device>_forecast` for consumption models). */
export function learnedDeviceId(item: { id: string, device_id?: string }): string {
  return item.device_id ?? item.id.replace(/_forecast$/, '')
}

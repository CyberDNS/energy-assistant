// The devices of the live status, each merged with its active setpoint and
// sorted by category, for the device cards on Home and the device pages.
import type { Ref } from 'vue'
import type { Status, StatusDevice } from './usePowerFlow'
import { computed } from 'vue'

const CATEGORY_ORDER: Record<string, number> = {
  battery: 0, charger: 1, vehicle: 2, thermal_load: 3, switch: 4, consumer: 5, pv: 6, meter: 7, sensor: 8,
}

export type LiveDevice = StatusDevice & { sp?: Record<string, unknown> }

export function useLiveDevices(status: Ref<Status | null>, categories?: string[]) {
  return computed<LiveDevice[]>(() => {
    const setpoints = new Map(((status.value?.setpoints ?? []) as { device_id: string }[]).map(s => [s.device_id, s]))
    return (status.value?.devices ?? [])
      .filter(d => !categories || categories.includes(d.category ?? ''))
      .map(d => ({ ...d, sp: setpoints.get(d.device_id) }))
      .sort((a, b) =>
        (CATEGORY_ORDER[a.category ?? ''] ?? 9) - (CATEGORY_ORDER[b.category ?? ''] ?? 9)
        || (a.label ?? a.device_id).localeCompare(b.label ?? b.device_id))
  })
}

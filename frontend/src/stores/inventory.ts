// What the house has, for parts of the shell that depend on it (navigation,
// status bar).  One shared copy, refreshed with every new plan.
import { ref, watch } from 'vue'
import { fetchVehicles } from '../api.js'
import { planVersion } from '../composables/useStatus.js'

export interface Vehicle {
  vehicle_id: string
  label: string
  [key: string]: unknown
}

export const vehicles = ref<Vehicle[]>([])
/** False until the first answer, so the shell does not flash "no cars". */
export const inventoryLoaded = ref(false)

let started = false

export async function refreshInventory(): Promise<void> {
  try {
    const list = await fetchVehicles()
    vehicles.value = Array.isArray(list) ? list : []
    inventoryLoaded.value = true
  } catch {
    /* keep what we have; the status banner reports an unreachable server */
  }
}

export function startInventory(): void {
  if (started) return
  started = true
  refreshInventory()
  watch(planVersion, refreshInventory)
  setInterval(refreshInventory, 5 * 60_000)
}

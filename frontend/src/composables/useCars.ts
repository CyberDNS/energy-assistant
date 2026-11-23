// Cars, chargers and the planned charging per car, kept fresh: every minute
// and the moment the server publishes a new plan.
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchChargers, fetchControllable, fetchVehicles } from '../api.js'
import { vehicles as sharedVehicles } from '../stores/inventory'
import { planVersion } from './useStatus.js'

/* eslint-disable @typescript-eslint/no-explicit-any */
export function useCars() {
  const cars = ref<any[]>([])
  const chargers = ref<any[]>([])
  const controllable = ref<any[]>([])
  const loaded = ref(false)
  let timer: ReturnType<typeof setInterval> | undefined

  async function refresh() {
    try {
      const [evs, chs, ctrl] = await Promise.all([
        fetchVehicles(), fetchChargers().catch(() => []), fetchControllable().catch(() => null),
      ])
      cars.value = Array.isArray(evs) ? evs : []
      sharedVehicles.value = cars.value
      chargers.value = Array.isArray(chs) ? chs : []
      controllable.value = ctrl?.devices ?? []
    } catch (e) {
      console.error('cars refresh failed', e)
    } finally {
      loaded.value = true
    }
  }

  /** The planned charging of a car (its entry in /api/controllable). */
  const planOf = (vehicleId: string) => controllable.value.find(d => d.vehicle_id === vehicleId) ?? null

  onMounted(() => { refresh(); timer = setInterval(refresh, 60_000) })
  onUnmounted(() => clearInterval(timer))
  watch(planVersion, refresh)

  return { cars, chargers, controllable, loaded, refresh, planOf }
}

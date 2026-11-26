import { ref } from 'vue'
import { fetchSavings } from '../api.js'

// Projected savings (observe mode, issue #134): what Energy Assistant would
// have saved had it controlled the devices it only observes — a simulation,
// see core/projection.py.  ``null`` before the first projected slot.
/* eslint-disable @typescript-eslint/no-explicit-any */
export interface ProjectedSavings {
  observed: string[]
  since: string
  /** Since the first projected slot: ``real`` is the saving, ``components`` per group. */
  all_time: { real: number, components: Record<string, number>, hours: number }
}

const projected = ref<ProjectedSavings | null>(null)
const loaded = ref(false)
let pending: Promise<void> | null = null

export function useProjectedSavings() {
  async function load() {
    pending ??= (async () => {
      try {
        projected.value = (await fetchSavings(24))?.projected ?? null
      } catch { /* keep the last value */ }
      loaded.value = true
      pending = null
    })()
    return pending
  }
  /** Projected saving of one savings group ("batteries", a charger id, a device id). */
  const ofGroup = (group: string): number | null => projected.value?.all_time.components[group] ?? null
  return { projected, loaded, load, ofGroup }
}

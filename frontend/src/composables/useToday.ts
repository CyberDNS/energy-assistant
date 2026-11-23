// Today in the house: what was measured since midnight, and what the plan and
// the forecasts say for the rest of the day.  For the Home page.
import type { Ref } from 'vue'
import type { Status } from './usePowerFlow'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchConfig, fetchControllable, fetchForecast, fetchHistory, fetchPlan, fetchSavingsSince } from '../api.js'
import { startOfToday } from '../lib/plotTime'
import { planVersion } from './useStatus.js'

/* eslint-disable @typescript-eslint/no-explicit-any */
interface Row { t: string, power_w: number | null, soc_pct: number | null, pos_w: number | null, neg_w: number | null }

export interface TodayPoint {
  t: string
  solarKw: number
  homeKw: number
  socPct: number | null
}

export function useToday(status: Ref<Status | null>) {
  const measured = ref<TodayPoint[]>([])
  const planned = ref<TodayPoint[]>([])
  const prices = ref<{ t: string, price: number, estimated: boolean }[]>([])
  const energy = ref<{ solar: number, imported: number, exported: number, used: number } | null>(null)
  const savings = ref<{ saving: number, cost: number } | null>(null)
  const controllable = ref<any[]>([])
  const loaded = ref(false)
  let config: any = null
  let timer: ReturnType<typeof setInterval> | undefined

  const ids = (category: string) => (status.value?.devices ?? []).filter(d => d.category === category).map(d => d.device_id)

  async function loadMeasured() {
    if (!status.value) return
    config ??= await fetchConfig().catch(() => null)
    // The meter at the grid connection is the one behind no other meter.
    const grid = (config?.devices ?? []).find((d: any) => d.category === 'meter' && !d.metered_by)?.device_id
    const pv = ids('pv'), bat = ids('battery'), ev = ids('charger')
    const midnight = startOfToday()
    const hours = Math.max((Date.now() - midnight.getTime()) / 3600_000, 0.2)
    const resp = await fetchHistory(hours.toFixed(2), [...(grid ? [grid] : []), ...pv, ...bat, ...ev])
    const m: Record<string, Row[]> = resp.measurements ?? {}
    const bucketH = (resp.bucket_minutes ?? 5) / 60

    // Per bucket: the sum over a group of devices.
    const byTime = new Map<string, { grid: number, pv: number, bat: number, ev: number, soc: number[], seen: boolean }>()
    const at = (t: string) => {
      let b = byTime.get(t)
      if (!b) byTime.set(t, b = { grid: 0, pv: 0, bat: 0, ev: 0, soc: [], seen: false })
      return b
    }
    const add = (list: string[], key: 'grid' | 'pv' | 'bat' | 'ev') => {
      for (const id of list) {
        for (const r of m[id] ?? []) {
          if (new Date(r.t) < midnight) continue
          const b = at(r.t)
          if (r.power_w != null) b[key] += r.power_w
          if (key === 'grid' && r.power_w != null) b.seen = true
          if (key === 'bat' && r.soc_pct != null) b.soc.push(r.soc_pct)
        }
      }
    }
    add(grid ? [grid] : [], 'grid'); add(pv, 'pv'); add(bat, 'bat'); add(ev, 'ev')

    const points: TodayPoint[] = []
    let used = 0
    for (const t of [...byTime.keys()].sort()) {
      const b = byTime.get(t)!
      // Load convention: production is negative.  What the house itself uses is
      // what comes from the grid and PV and is not taken by batteries and cars.
      const home = b.seen ? Math.max(b.grid - b.pv - b.bat - b.ev, 0) : 0
      used += (home + Math.max(b.ev, 0)) * bucketH / 1000
      points.push({
        t, solarKw: Math.max(-b.pv, 0) / 1000, homeKw: home / 1000,
        socPct: b.soc.length ? b.soc.reduce((x, y) => x + y, 0) / b.soc.length : null,
      })
    }
    measured.value = points
    const kwh = (list: string[], key: 'pos_w' | 'neg_w') => list.reduce((sum, id) => sum
      + (m[id] ?? []).filter(r => new Date(r.t) >= midnight).reduce((a, r) => a + (r[key] ?? 0), 0), 0) * bucketH / 1000
    energy.value = grid || pv.length
      ? { solar: kwh(pv, 'neg_w'), imported: kwh(grid ? [grid] : [], 'pos_w'), exported: kwh(grid ? [grid] : [], 'neg_w'), used }
      : null
  }

  async function loadPlanned() {
    const [plan, forecast, ctrl] = await Promise.all([fetchPlan(), fetchForecast(), fetchControllable().catch(() => null)])
    controllable.value = ctrl?.devices ?? []
    const end = startOfToday().getTime() + 24 * 3600_000
    // Stored energy over all batteries per plan step, as a share of their capacity.
    const caps: Record<string, { capacity_kwh?: number }> = forecast.storage_capacity ?? {}
    const capacity = Object.values(caps).reduce((a, c) => a + (c.capacity_kwh ?? 0), 0)
    const stored = new Map<string, number>()
    for (const i of plan.intents ?? []) {
      if (i.stored_energy_kwh != null && caps[i.device_id]?.capacity_kwh) {
        stored.set(i.timestep, (stored.get(i.timestep) ?? 0) + i.stored_energy_kwh)
      }
    }
    const solved = new Map<string, number>((plan.flows ?? []).map((f: any) => [f.timestep, f.pv_kw]))
    const ts: string[] = forecast.timestamps ?? []
    planned.value = ts
      .map((t, i): TodayPoint => ({
        t, solarKw: solved.get(t) ?? forecast.pv_kw[i] ?? 0, homeKw: forecast.consumption_kw[i] ?? 0,
        socPct: capacity && stored.has(t) ? stored.get(t)! / capacity * 100 : null,
      }))
      .filter(p => new Date(p.t).getTime() < end)
    const variable: number[] = forecast.variable_prices?.length ? forecast.variable_prices : (forecast.prices ?? [])
    const estimated: boolean[] = forecast.variable_price_is_estimated ?? []
    prices.value = ts.map((t, i) => ({ t, price: variable[i], estimated: !!estimated[i] })).filter(p => p.price != null)
  }

  async function loadSavings() {
    try {
      const resp = await fetchSavingsSince(startOfToday())
      savings.value = resp.totals ? { saving: resp.totals.real ?? 0, cost: resp.totals.real_cost ?? 0 } : null
    } catch {
      savings.value = null
    }
  }

  async function refresh() {
    await Promise.allSettled([loadMeasured(), loadPlanned(), loadSavings()])
    loaded.value = true
  }

  onMounted(() => { refresh(); timer = setInterval(refresh, 5 * 60_000) })
  onUnmounted(() => clearInterval(timer))
  watch(planVersion, () => { loadPlanned() })
  // The first status tells which devices exist.
  const stop = watch(status, (s) => { if (s) { stop(); loadMeasured() } })

  /** Share of today's consumption that did not come from the grid. */
  const selfSufficiency = computed(() => {
    const e = energy.value
    return e && e.used > 0.05 ? Math.min(1, Math.max(0, 1 - e.imported / e.used)) : null
  })

  return { measured, planned, prices, energy, savings, selfSufficiency, controllable, loaded, refresh }
}

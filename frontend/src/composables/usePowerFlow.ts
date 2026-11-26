// The house's live power flow, derived from the status snapshot.
// Load convention: consumption positive, production negative; grid positive = import.
import type { Ref } from 'vue'
import { computed } from 'vue'

export const IDLE_W = 10 // below this magnitude a flow is shown as idle

export interface StatusDevice {
  device_id: string
  label?: string
  category?: string | null
  power_w?: number | null
  soc_pct?: number | null
  available?: boolean
  /** Observe mode (#134): 'observe' when every device in control_targets is observed. */
  control?: 'observe' | 'active' | null
  /** The controlled devices whose observe/active switch concerns this device:
   *  itself, the thermal load a switch executes, the charger of a car. */
  control_targets?: string[]
}

export interface LedgerEntry {
  device_id: string
  stored_energy_kwh: number
  capacity_kwh: number
  cost_basis_eur_per_kwh: number
}

export interface FailSafeEvent {
  at: string
  kind: 'released' | 'taken_over'
  reason: string
  detail?: string
}

/** Fail-safe state (core/failsafe.py): devices released to their own operation. */
export interface FailSafeStatus {
  released: boolean
  reason?: string | null
  reason_text?: string | null
  detail?: string
  since?: string | null
  events?: FailSafeEvent[]
}

/** running, or waiting for a working config.yaml (nothing is planned or controlled). */
export interface ConfigurationState {
  state: 'running' | 'configuration_needed'
  problems: string[]
  devices: number
  homeassistant: { source: 'supervisor' | 'config', label: string } | null
}

/** Power-dependent grid charges: the running 15-minute interval and the month's peak. */
export interface PowerChargeStatus {
  interval_start: string
  interval_import_kwh: number
  interval_average_kw: number | null
  month: string
  month_peak_kw: number
  month_peak_at: string | null
  /** Average import an interval may have without any power charge. */
  free_kw: number | null
  thresholds: { threshold_kw: number, excess_eur_per_kwh: number }[]
  peaks: { eur_per_kw_month: number, minimum_kw: number }[]
  allowed_import_w: number | null
  planned_import_kw: number | null
  /** Devices held back by the power charges → their setpoint (W). */
  curtailed: Record<string, number>
}

export interface Status {
  timestamp: string
  configuration?: ConfigurationState
  planning?: boolean
  dry_run?: boolean
  failsafe?: FailSafeStatus
  grid_power_w?: number | null
  current_price_eur_per_kwh?: number | null
  pv_opportunity_price_eur_per_kwh?: number | null
  devices?: StatusDevice[]
  /** 'observe' | 'active' per device Energy Assistant commands (#134). */
  control?: Record<string, 'observe' | 'active'>
  ledger?: LedgerEntry[]
  power_charges?: PowerChargeStatus | null
  [key: string]: unknown
}

export type FlowTone = 'ok' | 'bad' | 'accent' | 'muted'

export interface FlowTile {
  id: 'solar' | 'grid' | 'battery' | 'ev' | 'home'
  label: string
  w: number
  dir: string
  tone: FlowTone
}

export function usePowerFlow(status: Ref<Status | null>) {
  const devices = computed(() => status.value?.devices ?? [])
  const categories = computed(() => new Set(devices.value.map(d => d.category).filter((c): c is string => !!c)))

  const tiles = computed<FlowTile[]>(() => {
    const s = status.value
    if (!s || !categories.value.size) return [] // no status yet, or a backend without category info
    const sum = (category: string) => devices.value
      .filter(d => d.category === category && d.power_w != null)
      .reduce((a, d) => a + (d.power_w ?? 0), 0)

    const pv = sum('pv')
    const bat = sum('battery')
    const ev = sum('charger')
    const grid = s.grid_power_w ?? 0
    const home = grid - pv - bat - ev

    const out: FlowTile[] = [
      { id: 'solar', label: 'Solar', w: Math.abs(pv),
        dir: pv < -IDLE_W ? 'producing' : 'idle', tone: pv < -IDLE_W ? 'ok' : 'muted' },
      { id: 'grid', label: 'Grid', w: Math.abs(grid),
        dir: grid > IDLE_W ? 'importing' : grid < -IDLE_W ? 'exporting' : 'idle',
        tone: grid > IDLE_W ? 'bad' : grid < -IDLE_W ? 'ok' : 'muted' },
    ]
    if (categories.value.has('battery')) {
      out.push({ id: 'battery', label: 'Battery', w: Math.abs(bat),
        dir: bat > IDLE_W ? 'charging' : bat < -IDLE_W ? 'discharging' : 'idle',
        tone: bat > IDLE_W ? 'ok' : bat < -IDLE_W ? 'accent' : 'muted' })
    }
    if (categories.value.has('charger')) {
      out.push({ id: 'ev', label: 'Cars', w: Math.abs(ev),
        dir: ev > IDLE_W ? 'charging' : 'idle', tone: ev > IDLE_W ? 'ok' : 'muted' })
    }
    out.push({ id: 'home', label: 'Home', w: Math.max(home, 0), dir: 'consuming', tone: 'muted' })
    return out
  })

  /** State of charge over all batteries, by stored energy. */
  const batterySocPct = computed<number | null>(() => {
    const ledger = (status.value?.ledger ?? []).filter(l => l.capacity_kwh > 0)
    if (ledger.length) {
      const stored = ledger.reduce((a, l) => a + l.stored_energy_kwh, 0)
      const capacity = ledger.reduce((a, l) => a + l.capacity_kwh, 0)
      return (stored / capacity) * 100
    }
    const socs = devices.value.filter(d => d.category === 'battery' && d.soc_pct != null).map(d => d.soc_pct as number)
    return socs.length ? socs.reduce((a, b) => a + b, 0) / socs.length : null
  })

  return { devices, categories, tiles, batterySocPct }
}

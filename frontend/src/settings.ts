// The categories of the Settings area.  `sections` are config.yaml sections
// shown as forms; `panel` picks a dedicated editor.
import type { Component } from 'vue'
import { Cable, CloudSun, Cpu, KeyRound, Palette, PlugZap, ReceiptEuro } from '@lucide/vue'

export interface SettingsPage {
  id: string
  title: string
  description: string
  icon: Component
  /** Tailwind classes of the icon tile. */
  tone: string
  panel: 'devices' | 'collection' | 'sections' | 'secrets' | 'interface'
  sections?: string[]
  /** Stored in this browser rather than in config.yaml. */
  local?: boolean
}

export const SETTINGS_PAGES: SettingsPage[] = [
  { id: 'devices', title: 'Devices', icon: PlugZap, tone: 'bg-emerald-600', panel: 'devices',
    description: 'Meters, PV, batteries, cars, chargers and loads, and how they are connected' },
  { id: 'tariffs', title: 'Tariffs', icon: ReceiptEuro, tone: 'bg-amber-600', panel: 'collection',
    description: 'What electricity costs to import and what export pays' },
  { id: 'forecasts', title: 'Forecasts', icon: CloudSun, tone: 'bg-sky-600', panel: 'collection',
    description: 'Where PV production and consumption forecasts come from' },
  { id: 'connections', title: 'Connections', icon: Cable, tone: 'bg-violet-600', panel: 'sections',
    sections: ['backends'],
    description: 'Home Assistant, MQTT and other systems the devices are reached through' },
  { id: 'system', title: 'System', icon: Cpu, tone: 'bg-slate-600', panel: 'sections',
    sections: ['optimizer', 'controller', 'energy_dashboard', 'server', 'logging'],
    description: 'Optimizer, control loop and dry run, web server and logging' },
  { id: 'secrets', title: 'Secrets', icon: KeyRound, tone: 'bg-rose-600', panel: 'secrets',
    description: 'Passwords and tokens kept in secrets.yaml' },
  { id: 'interface', title: 'User interface', icon: Palette, tone: 'bg-orange-500', panel: 'interface',
    local: true,
    description: 'Theme and how the app behaves inside Home Assistant, for this device' },
]

export function settingsPage(id: string | undefined): SettingsPage | undefined {
  return SETTINGS_PAGES.find(p => p.id === id)
}

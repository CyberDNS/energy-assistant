// The navigation of the app: one registry for the sidebar, page titles and
// breadcrumbs.  To add a page, add its route (router/index.ts) and an item
// here; `when` hides an item while the house has nothing to show there.
import type { Component } from 'vue'
import {
  BatteryMedium, BrainCircuit, CalendarClock, Car, Heater, History, House, PiggyBank, Settings,
} from '@lucide/vue'

export interface NavContext {
  /** Device categories present in the live status ("battery", "charger", …). */
  categories: Set<string>
  vehicleCount: number
}

export interface NavItem {
  id: string
  title: string
  to: string
  icon: Component
  when?: (ctx: NavContext) => boolean
}

export interface NavGroup {
  id: string
  title: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'overview',
    title: 'Overview',
    items: [
      { id: 'home', title: 'Home', to: '/', icon: House },
      { id: 'plan', title: 'Plan', to: '/plan', icon: CalendarClock },
    ],
  },
  {
    id: 'devices',
    title: 'Devices',
    items: [
      { id: 'cars', title: 'Cars', to: '/cars', icon: Car,
        when: ctx => ctx.vehicleCount > 0 || ctx.categories.has('charger') },
      { id: 'batteries', title: 'Batteries', to: '/batteries', icon: BatteryMedium,
        when: ctx => ctx.categories.has('battery') },
      { id: 'loads', title: 'Heating & loads', to: '/loads', icon: Heater,
        when: ctx => ctx.categories.has('thermal_load') || ctx.categories.has('switch') },
    ],
  },
  {
    id: 'insights',
    title: 'Insights',
    items: [
      { id: 'savings', title: 'Savings', to: '/savings', icon: PiggyBank },
      { id: 'history', title: 'History', to: '/history', icon: History },
      { id: 'learning', title: 'Learning', to: '/learning', icon: BrainCircuit },
    ],
  },
  {
    id: 'system',
    title: 'System',
    items: [
      { id: 'settings', title: 'Settings', to: '/settings', icon: Settings },
    ],
  },
]

/** The groups with the items that apply to this house; empty groups drop out. */
export function visibleNav(ctx: NavContext): NavGroup[] {
  return NAV_GROUPS
    .map(group => ({ ...group, items: group.items.filter(item => !item.when || item.when(ctx)) }))
    .filter(group => group.items.length > 0)
}

export function navItem(id: string): NavItem | undefined {
  for (const group of NAV_GROUPS) {
    const item = group.items.find(i => i.id === id)
    if (item) return item
  }
  return undefined
}

import { describe, expect, it } from 'vitest'
import { NAV_GROUPS, visibleNav } from './nav'

const ids = (ctx: { categories: string[], vehicleCount?: number }) =>
  visibleNav({ categories: new Set(ctx.categories), vehicleCount: ctx.vehicleCount ?? 0 })
    .flatMap(g => g.items.map(i => i.id))

describe('navigation', () => {
  it('hides the device pages a house has nothing for', () => {
    expect(ids({ categories: ['meter', 'pv'] })).toEqual(['home', 'plan', 'savings', 'history', 'learning', 'settings'])
    // …and with them the empty "Devices" group.
    expect(visibleNav({ categories: new Set(), vehicleCount: 0 }).map(g => g.id)).toEqual(['overview', 'insights', 'system'])
  })

  it('shows a device page as soon as such a device exists', () => {
    expect(ids({ categories: ['battery'] })).toContain('batteries')
    expect(ids({ categories: ['thermal_load'] })).toContain('loads')
    expect(ids({ categories: ['switch'] })).toContain('loads')
    expect(ids({ categories: ['charger'] })).toContain('cars')
    expect(ids({ categories: [], vehicleCount: 1 })).toContain('cars')
  })

  it('has unique ids and routes', () => {
    const items = NAV_GROUPS.flatMap(g => g.items)
    expect(new Set(items.map(i => i.id)).size).toBe(items.length)
    expect(new Set(items.map(i => i.to)).size).toBe(items.length)
  })
})

import { describe, expect, it } from 'vitest'
import { fmt, fmtAgo, fmtPower, fmtPrice } from './format'

describe('format', () => {
  it('switches from W to kW at 1000 W, keeping the sign', () => {
    expect(fmtPower(850)).toBe('850 W')
    expect(fmtPower(2310)).toBe('2.31 kW')
    expect(fmtPower(-1500)).toBe('-1.50 kW')
    expect(fmtPower(null)).toBe('—')
  })

  it('shows prices in cents', () => {
    expect(fmtPrice(0.284)).toBe('28.40 ct')
    expect(fmtPrice(undefined)).toBe('—')
  })

  it('shows missing numbers as a dash', () => {
    expect(fmt(null)).toBe('—')
    expect(fmt(12.345, 1)).toBe('12.3')
  })

  it('describes the age of a timestamp', () => {
    const now = Date.parse('2026-11-22T12:00:00Z')
    expect(fmtAgo('2026-11-22T11:59:48Z', now)).toBe('12s ago')
    expect(fmtAgo('2026-11-22T11:55:00Z', now)).toBe('5 min ago')
    expect(fmtAgo(null, now)).toBeNull()
  })
})

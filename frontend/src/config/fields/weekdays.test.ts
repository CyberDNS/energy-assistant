import { describe, expect, it } from 'vitest'
import { formatDays, parseDays } from './weekdays.js'

const days = (v: unknown): number[] => [...(parseDays(v) ?? [])].sort()

describe('weekdays', () => {
  it('parses names and ranges, also over the week end', () => {
    expect(days(['mon-fri'])).toEqual([0, 1, 2, 3, 4])
    expect(days(['fri-mon'])).toEqual([0, 4, 5, 6])
    expect(days('sat')).toEqual([5])
    expect(days(undefined)).toEqual([0, 1, 2, 3, 4, 5, 6])
    expect(parseDays(['monday'])).toBeNull()
  })
  it('formats runs as ranges and every day as unset', () => {
    expect(formatDays(new Set([0, 1, 2, 3, 4, 6]))).toEqual(['mon-fri', 'sun'])
    expect(formatDays(new Set([5, 6]))).toEqual(['sat', 'sun'])
    expect(formatDays(new Set([0, 1, 2, 3, 4, 5, 6]))).toBeUndefined()
    expect(formatDays(new Set())).toEqual([])
  })
})

import { describe, expect, it } from 'vitest'
import { buildWeek, parseTime, weekProblems } from './tou.js'
import { hasNestedFields, itemLabelOf, summaryOf } from './items.js'

const DAY_NIGHT = [
  { name: 'night', price_eur_per_kwh: 0.24, windows: [
    { days: ['mon-fri'], from: '22:00', to: '06:00' }, { days: ['sat', 'sun'], all_day: true }] },
  { name: 'day', price_eur_per_kwh: 0.32, default: true },
]

describe('time-of-use week', () => {
  it('reads quarter-hour times', () => {
    expect(parseTime('06:15')).toBe(25)
    expect(parseTime('24:00')).toBeNull()
    expect(parseTime('24:00', true)).toBe(96)
    expect(parseTime('06:10')).toBeNull()
  })
  it('fills the day/night example without problems', () => {
    const week = buildWeek(DAY_NIGHT)
    expect(week.table[0][0]).toBe(0)       // Mon 00:00 night (window over midnight)
    expect(week.table[0][24]).toBe(1)      // Mon 06:00 day (default)
    expect(week.table[0][88]).toBe(0)      // Mon 22:00 night
    expect(week.table[5][48]).toBe(0)      // Sat noon night (all day)
    expect(weekProblems(week)).toEqual([])
  })
  it('reports gaps without a default period', () => {
    const periods = [{ name: 'night', windows: [{ from: '22:00', to: '06:00' }] }]
    expect(weekProblems(buildWeek(periods))).toEqual(
      ['No price at Mon 06:00–22:00, Tue 06:00–22:00, Wed 06:00–22:00 and 4 more: add a time window or choose a period for all other times.'])
  })
  it('reports overlaps and two defaults', () => {
    const periods = [
      { name: 'a', default: true, windows: [{ days: ['mon'], from: '10:00', to: '12:00' }] },
      { name: 'b', default: true, windows: [{ days: ['mon'], from: '11:00', to: '13:00' }] },
    ]
    const problems = weekProblems(buildWeek(periods))
    expect(problems[0]).toMatch(/Only one period/)
    expect(problems[1]).toBe('a and b overlap: Mon 11:00–12:00.')
  })
})

describe('item names', () => {
  it('uses item_label, else the label in the singular', () => {
    expect(itemLabelOf({ item_label: 'time window', label: 'Windows' })).toBe('time window')
    expect(itemLabelOf({ label: 'Roof planes' })).toBe('roof plane')
    expect(itemLabelOf({ label: 'Charge curve (kW)' }, 'row')).toBe('row')
  })
  it('fills the summary template once its fields are set', () => {
    const spec = { summary: '{name} · {price_eur_per_kwh} €/kWh' }
    expect(summaryOf(spec, { name: 'day', price_eur_per_kwh: 0.3 })).toBe('day · 0.3 €/kWh')
    expect(summaryOf(spec, { name: 'day' })).toBe('day')
    expect(summaryOf(spec, {})).toBe('')
  })
})

describe('cards or table', () => {
  it('gives records with nested lists a card, others a table row', () => {
    expect(hasNestedFields({ name: { kind: 'str' }, windows: { kind: 'list', items: { kind: 'dict', fields: {} } } })).toBe(true)
    expect(hasNestedFields({ prices: { kind: 'dict', values: { kind: 'float' } } })).toBe(true)
    expect(hasNestedFields({ days: { kind: 'list', items: { kind: 'str' }, widget: 'weekdays' }, from: { kind: 'str' } })).toBe(false)
  })
})

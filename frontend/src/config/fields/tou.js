// The week of a time-of-use tariff as the config UI previews it: which
// period applies at each quarter hour, plus the gaps and overlaps.  Mirrors
// parse_schedule (plugins/time_of_use/schedule.py) for the table itself; the
// server still checks everything when the form is validated.
import { DAYS, parseDays } from './weekdays.js'

export const SLOTS_PER_DAY = 96
export const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

// "HH:MM" → quarter index (0 … 96), null when not a quarter-hour time.
export function parseTime(text, allow24 = false) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(text ?? '').trim())
  if (!m) return null
  const h = Number(m[1]), min = Number(m[2])
  if (h === 24 && min === 0 && allow24) return SLOTS_PER_DAY
  if (h > 23 || min > 59 || min % 15) return null
  return (h * 60 + min) / 15
}

export const formatQuarter = (q) => `${String(Math.floor(q / 4)).padStart(2, '0')}:${String((q % 4) * 15).padStart(2, '0')}`

// The (day, quarter) pairs one window covers; [] for an incomplete window.
function windowSlots(win) {
  if (!win || typeof win !== 'object') return []
  const days = parseDays(win.days)
  if (!days) return []
  let quarters
  if (win.all_day) {
    quarters = [...Array(SLOTS_PER_DAY).keys()]
  } else {
    const start = parseTime(win.from), end = parseTime(win.to, true)
    if (start == null || end == null || start === end % SLOTS_PER_DAY) return []
    quarters = start < end
      ? Array.from({ length: end - start }, (_, i) => start + i)
      : [...Array(end).keys(), ...Array.from({ length: SLOTS_PER_DAY - start }, (_, i) => start + i)]
  }
  return [...days].flatMap(d => quarters.map(q => [d, q]))
}

// periods (the config list) → { table, names, defaults, overlaps }:
//   table[day][quarter]  index of the period, null = not covered, -1 = overlap
//   overlaps             [{ a, b, slots: [[day, quarter], …] }]
export function buildWeek(periods) {
  const list = Array.isArray(periods) ? periods : []
  const table = DAYS.map(() => Array(SLOTS_PER_DAY).fill(null))
  const names = list.map((p, i) => (p?.name ? String(p.name) : `Period ${i + 1}`))
  const defaults = list.flatMap((p, i) => (p?.default ? [i] : []))
  const overlaps = new Map()
  list.forEach((p, idx) => {
    for (const win of Array.isArray(p?.windows) ? p.windows : []) {
      for (const [d, q] of windowSlots(win)) {
        const other = table[d][q]
        if (other === null) table[d][q] = idx
        else if (other !== idx) {
          const owner = other === -1 ? null : other
          const key = `${owner ?? '?'}|${idx}`
          if (!overlaps.has(key)) overlaps.set(key, { a: owner, b: idx, slots: [] })
          overlaps.get(key).slots.push([d, q])
          table[d][q] = -1
        }
      }
    }
  })
  if (defaults.length === 1) {
    for (const day of table) day.forEach((c, q) => { if (c === null) day[q] = defaults[0] })
  }
  return { table, names, defaults, overlaps: [...overlaps.values()] }
}

// One day of the table as runs of equal cells: [{ start, end, cell }].
export function runs(day) {
  const out = []
  day.forEach((cell, q) => {
    const last = out[out.length - 1]
    if (last && last.cell === cell) last.end = q + 1
    else out.push({ start: q, end: q + 1, cell })
  })
  return out
}

// Slots → "Mon 06:00–22:00, Tue 06:00–22:00 …" (at most `max` ranges).
export function describeSlots(slots, max = 3) {
  const byDay = DAYS.map(() => Array(SLOTS_PER_DAY).fill(false))
  for (const [d, q] of slots) byDay[d][q] = true
  const ranges = []
  byDay.forEach((day, d) => {
    for (const r of runs(day)) if (r.cell) ranges.push(`${DAY_NAMES[d]} ${formatQuarter(r.start)}–${formatQuarter(r.end)}`)
  })
  return ranges.slice(0, max).join(', ') + (ranges.length > max ? ` and ${ranges.length - max} more` : '')
}

// What is wrong with the week, in plain words (empty = every quarter hour of
// the week has exactly one price).
export function weekProblems(week) {
  const out = []
  if (week.defaults.length > 1) {
    out.push(`Only one period can apply at all other times; now: ${week.defaults.map(i => week.names[i]).join(', ')}.`)
  }
  for (const o of week.overlaps) {
    const who = o.a == null ? `${week.names[o.b]} and another period` : `${week.names[o.a]} and ${week.names[o.b]}`
    out.push(`${who} overlap: ${describeSlots(o.slots)}.`)
  }
  const gaps = []
  week.table.forEach((day, d) => day.forEach((c, q) => { if (c === null) gaps.push([d, q]) }))
  if (gaps.length && week.defaults.length <= 1) {
    out.push(`No price at ${describeSlots(gaps)}: add a time window or choose a period for all other times.`)
  }
  return out
}

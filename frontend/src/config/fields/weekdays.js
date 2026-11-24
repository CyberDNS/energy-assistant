// Weekday lists of a time-of-use window (`days: [mon-fri, sun]`), the same
// rules as the backend (plugins/time_of_use/schedule.py): day names mon … sun
// and ranges such as mon-fri (also over the week end, e.g. fri-mon).
export const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

// Value from the config → set of day indexes (0 = mon).  Unset = every day;
// null when it holds something that is not a day.
export function parseDays(value) {
  if (value == null) return new Set(DAYS.keys())
  const items = typeof value === 'string' ? [value] : value
  if (!Array.isArray(items)) return null
  const out = new Set()
  for (const item of items) {
    const [first, last, extra] = String(item).trim().toLowerCase().split('-')
    const a = DAYS.indexOf(first)
    const b = last === undefined ? a : DAYS.indexOf(last)
    if (a < 0 || b < 0 || extra !== undefined) return null
    for (let i = 0; i <= (b - a + 7) % 7; i++) out.add((a + i) % 7)
  }
  return out
}

// Set of day indexes → the shortest list of names and ranges (undefined =
// every day, the default).  Runs of three or more days become a range.
export function formatDays(set) {
  if (set.size === 7) return undefined
  const out = []
  let i = 0
  while (i < 7) {
    if (!set.has(i)) { i++; continue }
    let j = i
    while (j + 1 < 7 && set.has(j + 1)) j++
    if (j - i >= 2) out.push(`${DAYS[i]}-${DAYS[j]}`)
    else for (let k = i; k <= j; k++) out.push(DAYS[k])
    i = j + 1
  }
  return out
}

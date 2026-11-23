// Plotly shows date strings as they are, without converting time zones, so
// timestamps are turned into local wall-clock strings first.
const p = (n: number) => String(n).padStart(2, '0')

/** ISO timestamp → "2026-07-03 14:30:00" in local time. */
export function plotTime(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:00`
}

/** Local midnight of today as a Date. */
export function startOfToday(now = new Date()): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate())
}

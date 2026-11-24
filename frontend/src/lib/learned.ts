// What the UI says about a learned model (an entry of /api/learned),
// shared by the Learning overview and the device pages.

/* eslint-disable @typescript-eslint/no-explicit-any */
export interface LearnedKind {
  id: 'consumption' | 'pv' | 'thermal' | 'storage'
  title: string
  about: string
}

export const LEARNED_KINDS: LearnedKind[] = [
  { id: 'consumption', title: 'Consumption forecasts',
    about: 'Daily energy per consumer, from the outdoor temperature and the kind of day, spread over the day with a learned hourly shape. Feeds the consumption forecast.' },
  { id: 'pv', title: 'Solar forecast',
    about: 'The PV system learned from its measured production against past weather forecasts: orientation, tilt, peak power, shading and snow. Applied to the current weather forecast and corrected by the last hour. Feeds the PV forecast.' },
  { id: 'thermal', title: 'Heating and cooling behaviour',
    about: 'Devices kept between a minimum and a maximum (aquarium, fridge, AC): how fast the value drifts while idle and moves while running. Lets the planner shift their runs.' },
  { id: 'storage', title: 'Battery efficiency',
    about: 'Round-trip efficiency from closed cycles (rest to rest at the same state of charge): energy out ÷ energy in. Tells the planner what shifting energy really pays.' },
]

export function learnedKind(item: any): LearnedKind['id'] {
  return item.category ?? (item.kind === 'thermal' ? 'thermal' : item.kind === 'efficiency' ? 'storage'
    : item.kind === 'pv' ? 'pv' : 'consumption')
}

/** The status pill: whether planning uses what was learned. */
export function learnedStatus(item: any): { cls: string, text: string } {
  const s = item?.status
  if (item?.kind === 'efficiency') {
    if (s === 'learned') return { cls: 'pill-ok', text: 'Learned value in plan' }
    if (s === 'observing') return { cls: 'pill-warn', text: 'Learned, plan uses configured' }
    if (s === 'collecting') return { cls: 'pill-warn', text: `Collecting cycles (${item.estimate?.cycles ?? 0} of ${item.min_cycles})` }
    return { cls: 'pill-off', text: 'No closed cycle yet' }
  }
  if (item?.kind === 'thermal') {
    return s === 'learned'
      ? { cls: 'pill-ok', text: 'Learned model active' }
      : { cls: 'pill-warn', text: 'Using configured rates' }
  }
  if (s === 'fitted') return { cls: 'pill-ok', text: 'Model active' }
  if (s === 'fallback') return { cls: 'pill-warn', text: 'Using fallback profile' }
  return { cls: 'pill-off', text: 'No model' }
}

const num = (v: number | null | undefined, decimals = 1) => (v == null ? null : Number(v).toFixed(decimals))

/** One figure that says how good the model is, for the overview. */
export function learnedHeadline(item: any): string {
  if (item.kind === 'efficiency') {
    const rt = item.estimate?.round_trip
    return rt == null ? 'no estimate yet' : `${(rt * 100).toFixed(1)} % round trip`
  }
  if (item.kind === 'thermal') {
    const rmse = num(item.model?.rmse, 3)
    return rmse == null ? 'no model yet' : `±${rmse} ${item.unit ?? ''}/h`
  }
  const mae = num(item.model?.mae_kwh)
  return mae == null ? 'no model yet' : `±${mae} kWh/day`
}

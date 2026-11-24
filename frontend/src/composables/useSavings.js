import { ref, computed } from 'vue'
import { fetchSavings } from '../api.js'

// Savings against a simulated uncontrolled house (see core/savings.py).
// All values in €.

const C_SAVING   = '#4caf7d'
const C_LOSS     = '#e07070'
const C_UNCTRL   = '#9aa5a0'
const C_REAL     = '#6b7bb5'
const C_CAR      = '#d08a2e'
const C_PRICE    = '#ad7b00'

const HOUR_MS = 3600000
const DAY_MS  = 24 * HOUR_MS

// Rolling windows ending now; the server picks the bar size from the length.
const RANGES = [
  { hours: 24,   label: '24 h', tick: { dtick: 3 * HOUR_MS, tickformat: '%H:%M' } },
  { hours: 72,   label: '3 d',  tick: { dtick: 12 * HOUR_MS, tickformat: '%d.%m. %H:%M' } },
  { hours: 168,  label: '7 d',  tick: { dtick: DAY_MS, tickformat: '%d.%m.' } },
  { hours: 720,  label: '30 d', tick: { dtick: DAY_MS, tickformat: '%d.%m.' } },
  { hours: 2160, label: '90 d', tick: { dtick: 7 * DAY_MS, tickformat: '%d.%m.' } },
  { hours: 8760, label: '1 y',  tick: { dtick: 'M1', tickformat: '%b %y' } },
]

const BUCKET_LABEL = { hour: 'hour', day: 'day', week: 'week' }

function eur(v, signed = false) {
  if (v == null) return '—'
  const s = Math.abs(v).toFixed(2) + ' €'
  if (v < -0.005) return '−' + s
  return (signed && v > 0.005 ? '+' : '') + s
}

function kwh(v) {
  return v == null ? '—' : v.toFixed(1) + ' kWh'
}

// Plotly date axes ignore UTC offsets: give them local wall-clock time.
function localX(iso) {
  const d = new Date(iso)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function cumulative(values) {
  let acc = 0
  return values.map(v => (acc += v))
}

function storedTotals(house) {
  const devices = Object.values(house ?? {})
  return {
    kwh: devices.reduce((a, d) => a + (d.kwh ?? 0), 0),
    value: devices.reduce((a, d) => a + (d.value_eur ?? 0), 0),
  }
}

// Everything the savings charts need for one time range: the totals, the
// charts of the whole house, and one panel per controlled group (the
// batteries together, each car, each controlled device).  Used by the Savings
// page and by the "What it saved" section of a device page.
export function useSavings(initial = RANGES[2]) {
  const range  = ref(initial)
  const data   = ref(null)
  const loaded = ref(false)
  const error  = ref(null)

  // Range and data change together, so the charts render once per switch
  // (the new tick settings never meet the old data).
  const pending = ref(null)   // range clicked, data still loading
  let request = 0
  async function load(r) {
    const mine = ++request
    pending.value = r
    try {
      const resp = await fetchSavings(r.hours)
      if (mine !== request) return  // a newer click won
      data.value = resp
      range.value = r
      error.value = null
    } catch (e) {
      if (mine === request) error.value = String(e.message ?? e)
    }
    if (mine === request) pending.value = null
    loaded.value = true
  }

  const buckets = computed(() => data.value?.buckets ?? [])
  const totals  = computed(() => data.value?.totals ?? null)
  const allTime = computed(() => data.value?.all_time ?? null)
  const pct = computed(() => {
    const t = totals.value
    return t && t.uncontrolled_cost > 0.005 ? t.real / t.uncontrolled_cost * 100 : null
  })
  const stored = computed(() => ({
    real: storedTotals(data.value?.stored?.real),
    uncontrolled: storedTotals(data.value?.stored?.uncontrolled),
  }))
  const bucketLabel = computed(() => BUCKET_LABEL[data.value?.bucket] ?? 'period')

  const x = computed(() => buckets.value.map(b => localX(b.start)))
  const HOVER = computed(() =>
    `%{x|${range.value.tick.tickformat}}  %{y:.2f} €<extra>%{fullData.name}</extra>`)

  // Per-slot detail (24 h / 3 d): what the batteries and cars did.
  const slots = computed(() => data.value?.slots ?? null)
  const slotX = computed(() => (slots.value ?? []).map(r => localX(r.slot_start)))
  const SLOT_HOVER = '%{x|%d.%m. %H:%M}  %{y:.2f} €<extra>%{fullData.name}</extra>'

  const savingTraces = computed(() => {
    const perSlot = slots.value != null
    const y = perSlot ? slots.value.map(r => r.saving) : buckets.value.map(b => b.real)
    return [{
      name: 'Saving', type: 'bar', x: perSlot ? slotX.value : x.value, y,
      marker: { color: y.map(v => (v >= 0 ? C_SAVING : C_LOSS)) },
      hovertemplate: perSlot ? SLOT_HOVER : HOVER.value,
    }]
  })
  const savingTitle = computed(() =>
    slots.value != null ? '15 min' : bucketLabel.value)

  const storedText = house => (slots.value ?? []).map(r => {
    const d = r.stored_detail?.[house] ?? {}
    return Object.entries(d).map(([k, v]) => `${k}: ${v.toFixed(2)} kWh`).join('<br>')
  })
  const priceTrace = () => ({
    name: 'Import price', mode: 'lines', x: slotX.value, yaxis: 'y2',
    y: (slots.value ?? []).map(s => s.import_price),
    line: { shape: 'hv', width: 1.3, color: C_PRICE, dash: 'dot' },
    hovertemplate: '%{x|%H:%M}  %{y:.3f} €/kWh<extra>Import price</extra>',
  })

  // Batteries: stored energy of both houses; the gap between them is the
  // energy the plan held back (or used earlier) — shaded.
  const batteryTraces = computed(() => {
    const r = slots.value ?? []
    return [
      { name: 'Uncontrolled', mode: 'lines', x: slotX.value, y: r.map(s => s.uncontrolled_stored_kwh),
        line: { shape: 'hv', width: 2, color: C_UNCTRL, dash: 'dash' }, customdata: storedText('uncontrolled'),
        hovertemplate: '%{x|%H:%M}  %{y:.2f} kWh<br>%{customdata}<extra>Uncontrolled</extra>' },
      { name: 'Real (shaded: difference)', mode: 'lines', x: slotX.value, y: r.map(s => s.real_stored_kwh),
        fill: 'tonexty', fillcolor: 'rgba(107,123,181,.18)',
        line: { shape: 'hv', width: 2.5, color: C_REAL }, customdata: storedText('real'),
        hovertemplate: '%{x|%H:%M}  %{y:.2f} kWh<br>%{customdata}<extra>Real</extra>' },
      priceTrace(),
    ]
  })

  // One panel per controlled group (batteries, each car, each threshold
  // device), built from the groups the server reports.  Short windows show
  // what the group did per slot; longer ones the group's saving per bar.
  const groups = computed(() => data.value?.groups ?? [])
  const C_DEVICE = '#2f8f83'
  const groupSaving = id => totals.value?.components?.[id] ?? 0
  const breakdown = computed(() => {
    const comps = totals.value?.components ?? {}
    const byKind = { batteries: 0, car: 0, device: 0 }
    for (const g of groups.value) byKind[g.kind] = (byKind[g.kind] ?? 0) + (comps[g.id] ?? 0)
    return byKind
  })

  const carSlotTraces = g => {
    const r = slots.value ?? []
    const realK = r.map(s => s.detail?.evs?.[g.id]?.real_kwh ?? 0)
    const uncK = r.map(s => s.detail?.evs?.[g.id]?.unc_kwh ?? 0)
    if (![...realK, ...uncK].some(v => v > 0.01)) return []
    return [
      { name: 'Uncontrolled car', mode: 'lines', x: slotX.value, y: cumulative(uncK),
        line: { shape: 'hv', width: 2, color: C_CAR, dash: 'dash' },
        hovertemplate: '%{x|%H:%M}  %{y:.1f} kWh<extra>Uncontrolled car</extra>' },
      { name: 'Real car (shaded: difference)', mode: 'lines', x: slotX.value, y: cumulative(realK),
        fill: 'tonexty', fillcolor: 'rgba(208,138,46,.18)',
        line: { shape: 'hv', width: 2.5, color: C_CAR },
        hovertemplate: '%{x|%H:%M}  %{y:.1f} kWh<extra>Real car</extra>' },
      priceTrace(),
    ]
  }

  const deviceSlotTraces = g => {
    const r = slots.value ?? []
    const d = s => s.detail?.devices?.[g.id] ?? {}
    if (!r.some(s => d(s).real_value != null || d(s).unc_value != null)) return []
    const kw = (v, h) => (v == null || !h ? null : v / h)
    return [
      { name: 'Running, real', type: 'bar', x: slotX.value, yaxis: 'y2',
        y: r.map(s => kw(d(s).real_kwh, s.hours)), marker: { color: 'rgba(107,123,181,.30)' },
        width: 15 * 60000, offset: -15 * 60000,
        hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra>Running, real</extra>' },
      { name: 'Running, thermostat', type: 'bar', x: slotX.value, yaxis: 'y2',
        y: r.map(s => -(kw(d(s).unc_kwh, s.hours) ?? 0)), marker: { color: 'rgba(154,165,160,.35)' },
        width: 15 * 60000, offset: -15 * 60000,
        customdata: r.map(s => kw(d(s).unc_kwh, s.hours)),
        hovertemplate: '%{x|%H:%M}  %{customdata:.2f} kW<extra>Running, thermostat</extra>' },
      { name: 'Simulated thermostat', mode: 'lines', x: slotX.value, y: r.map(s => d(s).unc_value),
        line: { shape: 'linear', width: 2, color: C_UNCTRL, dash: 'dash' },
        hovertemplate: '%{x|%H:%M}  %{y:.2f} ' + (g.unit ?? '') + '<extra>Thermostat</extra>' },
      { name: 'Measured', mode: 'lines', x: slotX.value, y: r.map(s => d(s).real_value),
        line: { shape: 'linear', width: 2.5, color: C_DEVICE },
        hovertemplate: '%{x|%H:%M}  %{y:.2f} ' + (g.unit ?? '') + '<extra>Measured</extra>' },
    ]
  }

  const barMax = traces => Math.max(0.01, ...traces.filter(t => t.type === 'bar')
    .flatMap(t => t.y.map(v => Math.abs(v ?? 0))))

  const groupBarTraces = g => {
    const y = buckets.value.map(b => b.components?.[g.id] ?? 0)
    return [{
      name: 'Saving', type: 'bar', x: x.value, y,
      marker: { color: y.map(v => (v >= 0 ? C_SAVING : C_LOSS)) }, hovertemplate: HOVER.value,
    }]
  }

  const panels = computed(() => groups.value.map(g => {
    const short = slots.value != null
    let traces = [], layout_ = null, note = ''
    if (!short) {
      traces = groupBarTraces(g)
      layout_ = layout({ showlegend: false })
    } else if (g.kind === 'batteries') {
      traces = batteryTraces.value
      layout_ = slotLayout('kWh stored')
      note = 'Shaded: the difference — energy the plan held back for later (real above) or used earlier (real below)'
    } else if (g.kind === 'car') {
      traces = carSlotTraces(g)
      layout_ = slotLayout('kWh charged')
      note = traces.length ? 'Same energy, different timing; shaded: energy one car got earlier'
                           : 'Not charged in this window.'
    } else {
      traces = deviceSlotTraces(g)
      const lim = y => ({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: y, y1: y,
                          line: { color: C_LOSS, width: 1, dash: 'dot' } })
      layout_ = {
        ...layout(),
        yaxis: { title: g.unit || 'value' },
        // Running bars in a strip along the bottom: real above its zero line,
        // thermostat below, scaled to the device's power.
        yaxis2: { overlaying: 'y', side: 'right', showgrid: false,
                  range: [-1.2 * barMax(traces), 4 * barMax(traces)],
                  showticklabels: false, zeroline: true, zerolinecolor: 'rgba(0,0,0,.08)' },
        shapes: [lim(g.bottom_threshold), lim(g.top_threshold)],
        barmode: 'overlay',
        legend: { orientation: 'h', y: -0.22, font: { size: 10 } },
        margin: { t: 10, r: 30, b: 60, l: 50 },
      }
      note = traces.length
        ? 'Measured value vs. a plain thermostat between the thresholds (dotted). Bars: when each ran — real above, thermostat below.'
        : 'No readings in this window.'
    }
    return { ...g, saving: groupSaving(g.id), traces, layout: layout_, note,
             height: short ? (g.kind === 'car' ? '240px' : '280px') : '220px' }
  }))

  // Booked saving, and the same counting the saving still stored in the
  // batteries (real minus uncontrolled; gaps where older slots lack it).
  const cumulativeTraces = computed(() => {
    const booked = cumulative(buckets.value.map(b => b.real))
    const start = totals.value?.stored_position_start
    const withStored = buckets.value.map((b, i) =>
      start == null || b.stored_position_end == null ? null : booked[i] + b.stored_position_end - start)
    const traces = [{
      name: 'Saving', mode: 'lines', x: x.value, y: booked,
      line: { color: C_SAVING, width: 2.5, shape: 'hv' },
      fill: 'tozeroy', fillcolor: 'rgba(76,175,125,.12)', hovertemplate: HOVER.value,
    }]
    if (withStored.some(v => v != null)) traces.push({
      name: 'Incl. stored energy', mode: 'lines', x: x.value, y: withStored,
      line: { color: C_SAVING, width: 1.5, shape: 'hv', dash: 'dot' }, hovertemplate: HOVER.value,
    })
    return traces
  })

  const costTraces = computed(() => [
    { name: 'Uncontrolled house', type: 'bar', x: x.value,
      y: buckets.value.map(b => b.uncontrolled_cost),
      marker: { color: C_UNCTRL }, hovertemplate: HOVER.value },
    { name: 'Real house', type: 'bar', x: x.value,
      y: buckets.value.map(b => b.real_cost),
      marker: { color: C_REAL }, hovertemplate: HOVER.value },
  ])

  // One layout object per chart: Plotly writes the computed axis ranges
  // back into the layout it is given, so charts must not share axes.
  const layout = (extra = {}) => ({
    xaxis: {
      type: 'date', tickangle: 0, tickmode: 'linear',
      tick0: '2000-01-03', ...range.value.tick,
    },
    yaxis: { title: '€', zeroline: true, zerolinecolor: '#9aa5a0' },
    ...extra,
  })
  const savingLayout     = computed(() => layout({ showlegend: false }))
  const cumulativeLayout = computed(() => layout({
    showlegend: cumulativeTraces.value.length > 1,
    legend: { orientation: 'h', y: -0.15, font: { size: 10 } },
  }))
  const costLayout       = computed(() => layout())
  // Price on its own right axis, scaled to its range (not from 0) so the
  // variation over the window stays visible.
  const priceAxis = computed(() => {
    const p = (slots.value ?? []).map(s => s.import_price).filter(v => v != null)
    if (!p.length) return { overlaying: 'y', side: 'right', showgrid: false }
    const lo = Math.min(...p), hi = Math.max(...p), pad = (hi - lo) * 0.6 || 0.02
    return { title: '€/kWh', overlaying: 'y', side: 'right', showgrid: false,
             tickformat: '.2f', range: [lo - 3 * pad, hi + pad] }
  })
  const slotLayout = yTitle => ({
    ...layout(),
    yaxis: { title: yTitle, rangemode: 'tozero' },
    yaxis2: priceAxis.value,
    legend: { orientation: 'h', y: -0.22, font: { size: 10 } },
    margin: { t: 10, r: 60, b: 60, l: 50 },
  })

  return {
    RANGES, range, groupSaving, pending, data, loaded, error, load, eur, kwh,
    buckets, totals, allTime, pct, stored, bucketLabel,
    savingTraces, cumulativeTraces, costTraces, panels, breakdown, groups,
    slots, savingTitle, savingLayout, cumulativeLayout, costLayout,
  }
}

export { RANGES, eur, kwh }

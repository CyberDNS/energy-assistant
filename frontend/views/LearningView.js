import { defineComponent, ref, computed, onMounted } from 'vue'
import { fetchLearned, refitLearned } from '../api.js'
import PlotlyChart from '../components/PlotlyChart.js'

// One colour per thing, the same in every chart on this tab.
const C = {
  actual:   '#2a78d6',  // measured consumption
  model:    '#eb6834',  // what the model says
  outlier:  '#1baf7a',  // days the fit dropped on its own
  excluded: '#8d9891',  // days removed by exclude / exclude_when
  active:   '#6250d6',  // hourly shape on days the temperature term is active
  idle:     '#008300',  // hourly shape on the other days
  running:  'rgba(42,120,214,.16)',  // thermal: share of a window the device ran
  ambient:  '#8d9891',  // thermal: ambient input
  limit:    '#b8574a',  // thermal: thresholds
  power:    'rgba(42,120,214,.30)',  // efficiency: battery power
  cycle:    'rgba(235,104,52,.10)',  // efficiency: closed cycles
  configured: '#8d9891',             // efficiency: configured value
}

// One category per learning method.  Order = order of the tabs.
const CATEGORIES = [
  { id: 'consumption', label: 'Consumption',
    about: 'Daily energy per consumer, from the outdoor temperature and the kind of day, spread over the day with a learned hourly shape. Feeds the consumption forecast.' },
  { id: 'thermal', label: 'Min/Max devices',
    about: 'Devices kept between a minimum and a maximum (aquarium, fridge, AC): how fast the value drifts while idle and moves while running. Lets the planner shift their runs.' },
  { id: 'storage', label: 'Batteries',
    about: 'Round-trip efficiency from closed cycles (rest to rest at the same SoC): energy out ÷ energy in. Tells the planner what arbitrage really pays.' },
]
const categoryOf = i => i.category ?? (i.kind === 'thermal' ? 'thermal' : i.kind === 'efficiency' ? 'storage' : 'consumption')

const RANGES = [
  { days: 30,  label: '30 d' },
  { days: 90,  label: '90 d' },
  { days: 365, label: '1 y' },
  { days: 0,   label: 'All' },
]

const fmt = (v, dec = 1) => (v == null ? '—' : Number(v).toFixed(dec))

function toPlotlyDate(iso) {
  const d = new Date(iso)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:00:00`
}

// Thermal series are 15-minute points: keep the minutes.
function toPlotlyMinute(iso) {
  const d = new Date(iso)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:00`
}

function hinge(model, t) {
  return model.direction === 'cooling'
    ? Math.max(0, t - model.base_temp)
    : Math.max(0, model.base_temp - t)
}

export default defineComponent({
  name: 'LearningView',
  components: { PlotlyChart },

  setup() {
    const items    = ref([])
    const selected = ref(null)
    const category = ref(null)
    const loading  = ref(true)
    const error    = ref('')
    const busy     = ref(false)
    const rangeDays = ref(90)

    const categories = computed(() => CATEGORIES
      .map(c => ({ ...c, items: items.value.filter(i => categoryOf(i) === c.id) }))
      .filter(c => c.items.length))
    const cat = computed(() => categories.value.find(c => c.id === category.value) ?? categories.value[0] ?? null)
    const item  = computed(() => cat.value?.items.find(i => i.id === selected.value) ?? cat.value?.items[0] ?? null)
    function pickCategory(id) {
      category.value = id
      selected.value = categories.value.find(c => c.id === id)?.items[0]?.id ?? null
    }
    // Green: the learned model is what planning uses; amber: learning but not (yet) used.
    const itemDot = i => (['fitted', 'learned'].includes(i.status) ? 'dot-ok'
      : ['fallback', 'collecting', 'observing'].includes(i.status) ? 'dot-warn' : 'dot-off')
    const model = computed(() => item.value?.model ?? null)
    const isTemp = computed(() => model.value?.mode === 'temperature')
    const isThermal = computed(() => item.value?.kind === 'thermal')
    const isEfficiency = computed(() => item.value?.kind === 'efficiency')
    const itemLabel = i => (i.label ?? i.id.replace(/_forecast$/, ''))

    async function load() {
      try {
        items.value = await fetchLearned()
        if (!categories.value.some(c => c.id === category.value)) category.value = categories.value[0]?.id ?? null
        if (!cat.value?.items.some(i => i.id === selected.value)) selected.value = cat.value?.items[0]?.id ?? null
        error.value = ''
      } catch (e) {
        error.value = String(e.message ?? e)
      } finally {
        loading.value = false
      }
    }

    async function refit() {
      if (!item.value || busy.value) return
      busy.value = true
      try {
        await refitLearned(item.value.id)
        await load()
      } catch (e) {
        error.value = String(e.message ?? e)
      } finally {
        busy.value = false
      }
    }

    onMounted(load)

    // ── summary ──────────────────────────────────────────────────────────
    const counts = computed(() => {
      const c = { used: 0, outlier: 0, excluded: 0, no_temp: 0 }
      for (const d of item.value?.days ?? []) c[d.status] = (c[d.status] ?? 0) + 1
      return c
    })

    const formula = computed(() => {
      const m = model.value
      if (!m) return '—'
      if (m.mode === 'temperature') {
        const side = m.direction === 'cooling' ? 'above' : 'below'
        return `${fmt(m.floor_kwh)} kWh/day + ${fmt(m.slope_kwh_per_k, 2)} kWh per °C ${side} ${fmt(m.base_temp)} °C`
      }
      const l = m.level_kwh ?? {}
      if (l.weekday != null && l.weekend != null) {
        return `${fmt(l.weekday)} kWh on weekdays, ${fmt(l.weekend)} kWh on weekends`
      }
      return `${fmt(l.all)} kWh/day`
    })

    const fittedAt = computed(() => {
      const at = model.value?.fitted_at ?? item.value?.fitted_at
      return at ? new Date(at).toLocaleString() : '—'
    })

    const statusPill = computed(() => {
      const s = item.value?.status
      if (isEfficiency.value) {
        const est = item.value.estimate
        if (s === 'learned')    return { cls: 'pill-ok',   text: 'Learned value in plan' }
        if (s === 'observing')  return { cls: 'pill-warn', text: 'Learned — plan uses configured' }
        if (s === 'collecting') return { cls: 'pill-warn', text: `Collecting cycles (${est?.cycles ?? 0} of ${item.value.min_cycles})` }
        return { cls: 'pill-off', text: 'No closed cycle yet' }
      }
      if (isThermal.value) {
        return s === 'learned'
          ? { cls: 'pill-ok', text: 'Learned model active' }
          : { cls: 'pill-warn', text: 'Using configured rates' }
      }
      if (s === 'fitted')   return { cls: 'pill-ok',   text: 'Model active' }
      if (s === 'fallback') return { cls: 'pill-warn', text: 'Using fallback profile' }
      return { cls: 'pill-off', text: 'No model' }
    })

    // ── chart 1: daily energy against temperature ────────────────────────
    const scatterTraces = computed(() => {
      const m = model.value
      const days = (item.value?.days ?? []).filter(d => d.temp != null)
      if (!m || !isTemp.value || !days.length) return []
      const pts = (status, name, marker) => {
        const rows = days.filter(d => d.status === status)
        return {
          name: `${name} (${rows.length})`, type: 'scatter', mode: 'markers', marker,
          x: rows.map(d => d.temp), y: rows.map(d => d.kwh), text: rows.map(d => d.date),
          hovertemplate: '%{text}<br>%{x:.1f} °C · %{y:.1f} kWh<extra>' + name + '</extra>',
        }
      }
      const temps = days.map(d => d.temp)
      const lo = Math.floor(Math.min(...temps)), hi = Math.ceil(Math.max(...temps))
      const xs = []
      for (let t = lo; t <= hi; t += 0.5) xs.push(t)
      return [
        pts('excluded', 'Excluded', { color: C.excluded, size: 7, symbol: 'x-thin-open', line: { width: 1.5 } }),
        pts('used', 'Used for training', { color: C.actual, size: 6, opacity: 0.65 }),
        pts('outlier', 'Dropped as outlier', { color: C.outlier, size: 9, symbol: 'diamond-open', line: { width: 2 } }),
        {
          name: 'Model', type: 'scatter', mode: 'lines', line: { color: C.model, width: 2.5 },
          x: xs, y: xs.map(t => m.floor_kwh + m.slope_kwh_per_k * hinge(m, t)),
          hovertemplate: '%{x:.1f} °C · %{y:.1f} kWh<extra>Model</extra>',
        },
      ].filter(t => t.x.length)
    })

    // ── chart 2: hourly shape ────────────────────────────────────────────
    const shapeTraces = computed(() => {
      const shapes = model.value?.shapes ?? {}
      const hours = Array.from({ length: 24 }, (_, h) => h)
      const line = (key, name, color, dash) => shapes[key] && {
        name, type: 'scatter', mode: 'lines', line: { color, width: 2, dash },
        x: hours, y: shapes[key].map(v => v * 100),
        hovertemplate: '%{x}:00 · %{y:.1f} % of the day<extra>' + name + '</extra>',
      }
      const active = model.value?.direction === 'cooling' ? 'Cooling day' : 'Heating day'
      const traces = isTemp.value
        ? [
            line('active|weekday', `${active}, weekday`, C.active, 'solid') ?? line('active', active, C.active, 'solid'),
            line('active|weekend', `${active}, weekend`, C.active, 'dot'),
            line('idle|weekday', 'Mild day, weekday', C.idle, 'solid') ?? line('idle', 'Mild day', C.idle, 'solid'),
            line('idle|weekend', 'Mild day, weekend', C.idle, 'dot'),
          ]
        : [
            line('weekday', 'Weekday', C.active, 'solid') ?? line('all', 'All days', C.active, 'solid'),
            line('weekend', 'Weekend', C.active, 'dot'),
          ]
      return traces.filter(Boolean)
    })

    // ── chart 3: actual vs model over time ───────────────────────────────
    const seriesTraces = computed(() => {
      let days = item.value?.days ?? []
      if (!days.length) return []
      if (rangeDays.value) days = days.slice(-rangeDays.value)
      const trained = days.filter(d => d.status === 'used' || d.status === 'no_temp')
      const mark = (status, name, marker) => {
        const rows = days.filter(d => d.status === status)
        return {
          name, type: 'scatter', mode: 'markers', marker,
          x: rows.map(d => d.date), y: rows.map(d => d.kwh),
          hovertemplate: '%{x|%d.%m.%Y} · %{y:.1f} kWh<extra>' + name + '</extra>',
        }
      }
      const modelled = days.filter(d => d.model_kwh != null)
      return [
        {
          name: 'Actual', type: 'scatter', mode: 'lines', line: { color: C.actual, width: 2 },
          x: trained.map(d => d.date), y: trained.map(d => d.kwh), connectgaps: false,
          hovertemplate: '%{x|%d.%m.%Y} · %{y:.1f} kWh<extra>Actual</extra>',
        },
        {
          name: 'Model', type: 'scatter', mode: 'lines', line: { color: C.model, width: 2 },
          x: modelled.map(d => d.date), y: modelled.map(d => d.model_kwh),
          hovertemplate: '%{x|%d.%m.%Y} · %{y:.1f} kWh<extra>Model</extra>',
        },
        mark('excluded', 'Excluded', { color: C.excluded, size: 7, symbol: 'x-thin-open', line: { width: 1.5 } }),
        mark('outlier', 'Dropped as outlier', { color: C.outlier, size: 9, symbol: 'diamond-open', line: { width: 2 } }),
      ].filter(t => t.x.length)
    })

    // ── chart 4: forecast ────────────────────────────────────────────────
    const forecastTraces = computed(() => {
      const pts = item.value?.forecast?.points ?? []
      if (!pts.length) return []
      return [{
        name: 'Forecast', type: 'scatter', mode: 'lines',
        line: { color: C.model, width: 2, shape: 'hv' },
        x: pts.map(p => toPlotlyDate(p.t)), y: pts.map(p => p.kw),
        hovertemplate: '%{x|%a %H:%M} · %{y:.2f} kW<extra></extra>',
      }]
    })
    const forecastDays = computed(() => item.value?.forecast?.days ?? [])

    // ── thermal models (threshold devices) ───────────────────────────────
    const tUnit = computed(() => item.value?.unit ?? '')
    const rate = v => (v == null ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(3)} ${tUnit.value}/h`)
    const thermalModes = computed(() => Object.keys(item.value?.rates?.running_per_h ?? {}))
    const modeName = m => ({ cool: 'Cooling', heat: 'Heating' }[m] ?? m)
    const thermalForm = computed(() => {
      const m = model.value
      if (!m) return '—'
      if (m.form === 'ambient') {
        return `Pulled towards ambient at ${fmt(m.a * 100, 1)} %/h of the gap`
          + (m.ambient_mean != null ? ` (rates at ${fmt(m.ambient_mean)} ${tUnit.value} ambient)` : '')
      }
      return 'Constant rates (no ambient input)'
    })
    const thermalTraces = computed(() => {
      // Break the lines where history has a gap (missing windows).
      const rows = []
      let prev = null
      for (const r of item.value?.series ?? []) {
        const t = new Date(r.t).getTime()
        if (prev != null && t - prev > 16 * 60000) {
          rows.push({ t: new Date(prev + 60000).toISOString(), value: null, model: null, on: {}, ambient: null })
        }
        rows.push(r)
        prev = t
      }
      if (!rows.length) return []
      const x = rows.map(r => toPlotlyMinute(r.t))
      const on = rows.map(r => Object.values(r.on ?? {}).reduce((a, b) => a + b, 0))
      const traces = [
        // Each point ends its 15-minute window: draw the bar over that window.
        { name: 'Device running', type: 'bar', x, y: on, yaxis: 'y2', marker: { color: C.running },
          width: 15 * 60000, offset: -15 * 60000,
          hovertemplate: '%{x|%d.%m. %H:%M} · ran %{y:.0%} of 15 min<extra></extra>' },
        { name: 'Measured', type: 'scatter', mode: 'lines', x, y: rows.map(r => r.value),
          line: { color: C.actual, width: 2, shape: 'hv' },
          hovertemplate: '%{x|%d.%m. %H:%M} · %{y:.2f} ' + tUnit.value + '<extra>Measured</extra>' },
        { name: 'Model', type: 'scatter', mode: 'lines', x, y: rows.map(r => r.model),
          line: { color: C.model, width: 2, dash: 'dot' },
          hovertemplate: '%{x|%d.%m. %H:%M} · %{y:.2f} ' + tUnit.value + '<extra>Model</extra>' },
      ]
      if (rows.some(r => r.ambient != null)) {
        traces.push({ name: 'Ambient', type: 'scatter', mode: 'lines', x, y: rows.map(r => r.ambient),
          line: { color: C.ambient, width: 1.5 },
          hovertemplate: '%{x|%d.%m. %H:%M} · %{y:.2f} ' + tUnit.value + '<extra>Ambient</extra>' })
      }
      return traces
    })
    const thermalLayout = computed(() => {
      const it = item.value ?? {}
      const line = y => ({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: y, y1: y,
                           line: { color: C.limit, width: 1, dash: 'dash' } })
      return {
        xaxis: { type: 'date', tickformat: '%d.%m. %H:%M' },
        yaxis: { title: it.unit || 'value' },
        yaxis2: { overlaying: 'y', side: 'right', range: [0, 4], showgrid: false, showticklabels: false },
        shapes: [line(it.bottom_threshold), line(it.top_threshold)],
        barmode: 'overlay', bargap: 0,
      }
    })

    // ── battery efficiency ───────────────────────────────────────────────
    const pct = (v, dec = 1) => (v == null ? '—' : `${(v * 100).toFixed(dec)} %`)
    const est = computed(() => item.value?.estimate ?? null)
    const cyclesNewestFirst = computed(() => [...(item.value?.cycles ?? [])].reverse())
    // Cycle ends with a day of padding on both sides, so a single cycle
    // doesn't fill the axis and the reference lines stay visible.
    const cycleSpan = computed(() => {
      const ends = (item.value?.cycles ?? []).map(c => new Date(c.end).getTime())
      if (!ends.length) return undefined
      const day = 86400000
      return [toPlotlyMinute(new Date(Math.min(...ends) - day).toISOString()),
              toPlotlyMinute(new Date(Math.max(...ends) + day).toISOString())]
    })
    const cycleTraces = computed(() => {
      const cyc = item.value?.cycles ?? []
      if (!cyc.length) return []
      const x = cyc.map(c => toPlotlyMinute(c.end))
      const span = cycleSpan.value
      const flat = (v, name, color, dash) => ({
        name, type: 'scatter', mode: 'lines', x: span, y: [v * 100, v * 100],
        line: { color, width: 2, dash }, hoverinfo: 'skip',
      })
      const traces = [{
        name: 'Cycle', type: 'scatter', mode: 'markers', x, y: cyc.map(c => c.round_trip * 100),
        marker: { color: C.actual, size: cyc.map(c => 6 + 4 * Math.sqrt(c.energy_in_kwh)), opacity: 0.75 },
        customdata: cyc.map(c => [c.energy_in_kwh, c.energy_out_kwh, c.soc_pct]),
        hovertemplate: '%{x|%d.%m. %H:%M} · %{y:.1f} %<br>in %{customdata[0]:.2f} kWh · out %{customdata[1]:.2f} kWh'
          + ' · at %{customdata[2]:.0f} % SoC<extra>Cycle</extra>',
      }]
      if (est.value) traces.push(flat(est.value.round_trip, 'Learned', C.model, 'solid'))
      traces.push(flat(item.value.configured.round_trip, 'Configured', C.configured, 'dash'))
      return traces
    })
    const cycleLayout = computed(() => {
      const cyc = item.value?.cycles ?? []
      const vals = [...cyc.map(c => c.round_trip), item.value?.configured?.round_trip ?? 1]
      const lo = Math.max(0, Math.floor(Math.min(...vals) * 20) / 20 - 0.05)
      return {
        xaxis: { type: 'date', tickformat: '%d.%m.', range: cycleSpan.value },
        yaxis: { title: '% returned', range: [lo * 100, 102] },
        hovermode: 'closest',
      }
    })
    const socTraces = computed(() => {
      const rows = item.value?.series ?? []
      if (!rows.length) return []
      const x = rows.map(r => toPlotlyMinute(r.t))
      return [
        { name: 'Power (kW, + charging)', type: 'bar', x, y: rows.map(r => r.power_w / 1000), yaxis: 'y2',
          marker: { color: C.power }, width: 15 * 60000, offset: 0,
          hovertemplate: '%{x|%d.%m. %H:%M} · %{y:.2f} kW<extra>Power</extra>' },
        { name: 'SoC', type: 'scatter', mode: 'lines', x, y: rows.map(r => r.soc_pct),
          line: { color: C.actual, width: 2, shape: 'hv' },
          hovertemplate: '%{x|%d.%m. %H:%M} · %{y:.0f} %<extra>SoC</extra>' },
      ]
    })
    const socLayout = computed(() => {
      const rows = item.value?.series ?? []
      const first = rows.length ? new Date(rows[0].t).getTime() : 0
      const shapes = (item.value?.cycles ?? [])
        .filter(c => new Date(c.end).getTime() >= first)
        .map(c => ({ type: 'rect', xref: 'x', yref: 'paper', y0: 0, y1: 1,
                     x0: toPlotlyMinute(c.start), x1: toPlotlyMinute(c.end),
                     fillcolor: C.cycle, line: { width: 0 }, layer: 'below' }))
      // Symmetric power axis: its zero sits at 50 % SoC.
      const kw = Math.max(0.5, ...rows.map(r => Math.abs(r.power_w) / 1000)) * 1.05
      return {
        xaxis: { type: 'date', tickformat: '%d.%m. %H:%M' },
        yaxis: { title: 'SoC (%)', range: [0, 100] },
        yaxis2: { overlaying: 'y', side: 'right', showgrid: false, zeroline: true, range: [-kw, kw], ticksuffix: ' kW' },
        shapes, bargap: 0, margin: { t: 10, r: 55, b: 60, l: 50 },
      }
    })

    const excludedDays = computed(() =>
      (item.value?.days ?? []).filter(d => d.status === 'excluded' || d.status === 'outlier').reverse())

    return {
      items, selected, item, model, isTemp, isThermal, itemLabel, loading, error, busy, refit, load,
      categories, cat, pickCategory, itemDot, isEfficiency,
      pct, est, cyclesNewestFirst, cycleTraces, cycleLayout, socTraces, socLayout,
      tUnit, rate, thermalModes, modeName, thermalForm, thermalTraces, thermalLayout,
      counts, formula, fittedAt, statusPill, fmt,
      scatterTraces, shapeTraces, seriesTraces, forecastTraces, forecastDays, excludedDays,
      rangeDays, RANGES,
    }
  },

  template: `
    <div>
      <div v-if="loading" class="panel">Loading…</div>
      <div v-else-if="!items.length" class="panel">
        No learned models configured. Give a consumer device a
        <code>forecast:</code> section with <code>type: learned</code>, add a threshold
        device (aquarium cooler, AC, …) or a battery to see it here.
      </div>

      <template v-else-if="item">
        <div class="learn-cats">
          <button v-for="c in categories" :key="c.id"
            :class="['learn-cat', cat.id === c.id ? 'active' : '']"
            @click="pickCategory(c.id)">
            {{ c.label }} <span class="learn-count">{{ c.items.length }}</span>
          </button>
        </div>
        <div class="learn-about">{{ cat.about }}</div>

        <div class="learn-head">
          <div class="range-btns" style="margin-bottom:0; flex-wrap:wrap">
            <button v-for="i in cat.items" :key="i.id"
              :class="['range-btn', item.id === i.id ? 'active' : '']"
              @click="selected = i.id"><span :class="['learn-dot', itemDot(i)]"></span>{{ itemLabel(i) }}</button>
          </div>
          <span :class="['pill', statusPill.cls]">{{ statusPill.text }}</span>
          <span class="learn-spacer"></span>
          <span class="stamp">Fitted {{ fittedAt }}</span>
          <button class="btn btn-neutral" :disabled="busy" @click="refit">
            {{ busy ? 'Refitting…' : 'Refit now' }}
          </button>
        </div>

        <div v-if="error || item.last_error" class="ev-error" style="margin-bottom:8px">
          ⚠ {{ error || item.last_error }}
        </div>

        <template v-if="isEfficiency">
          <div class="kpis">
            <div class="card">
              <div class="k">Round trip, learned</div>
              <div class="v">{{ pct(est?.round_trip) }}</div>
              <div class="savings-sub" v-if="est">± {{ pct(est.spread) }} between cycles</div>
            </div>
            <div class="card">
              <div class="k">Round trip, configured</div>
              <div class="v">{{ pct(item.configured.round_trip) }}</div>
              <div class="savings-sub">{{ pct(item.configured.charge_efficiency, 0) }} charge · {{ pct(item.configured.discharge_efficiency, 0) }} discharge</div>
            </div>
            <div class="card">
              <div class="k">Per direction, learned</div>
              <div class="v">{{ pct(est?.efficiency) }}</div>
              <div class="savings-sub">√ round trip, charge = discharge</div>
            </div>
            <div class="card">
              <div class="k">Plan uses</div>
              <div class="v">{{ pct(item.planned.charge_efficiency) }}</div>
              <div class="savings-sub">{{ item.mode === 'learned' ? 'efficiency: learned' : 'efficiency: configured' }}</div>
            </div>
            <div class="card">
              <div class="k">Cycles used</div>
              <div class="v">{{ est?.cycles ?? 0 }}</div>
              <div class="savings-sub" v-if="est">{{ est.outliers }} outliers · over {{ fmt(est.window_days) }} days</div>
            </div>
            <div class="card">
              <div class="k">Throughput</div>
              <div class="v">{{ fmt(est?.energy_in_kwh) }} kWh</div>
              <div class="savings-sub" v-if="est">in · {{ fmt(est.energy_out_kwh) }} kWh out</div>
            </div>
          </div>

          <div class="row2">
            <div class="panel">
              <h2>Round trip per cycle</h2>
              <PlotlyChart :traces="cycleTraces" :layout="cycleLayout" height="300px" />
              <div class="footnote">
                One dot per closed cycle, at its end; bigger dots moved more energy and weigh more.
                Learned = all energy out ÷ all energy in, after dropping cycles far from the median.
              </div>
            </div>
            <div class="panel">
              <h2>Cycles ({{ item.cycles.length }})</h2>
              <div class="learn-scroll">
                <table>
                  <thead><tr><th>From</th><th>To</th><th>SoC</th><th>In</th><th>Out</th><th>Round trip</th></tr></thead>
                  <tbody>
                    <tr v-for="c in cyclesNewestFirst" :key="c.start">
                      <td>{{ new Date(c.start).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) }}</td>
                      <td>{{ new Date(c.end).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) }}</td>
                      <td>{{ fmt(c.soc_pct, 0) }} %</td>
                      <td>{{ fmt(c.energy_in_kwh, 2) }}</td>
                      <td>{{ fmt(c.energy_out_kwh, 2) }}</td>
                      <td>{{ pct(c.round_trip) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="footnote" v-if="!item.cycles.length">
                No closed cycle yet: the battery has to rest, charge and discharge at least
                {{ fmt(item.capacity_kwh * 0.15) }} kWh each way, and rest again at the same SoC reading.
              </div>
            </div>
          </div>

          <div class="panel full">
            <h2>SoC and power — {{ item.label }}</h2>
            <PlotlyChart :traces="socTraces" :layout="socLayout" height="320px" />
            <div class="footnote">
              Shaded: closed cycles. A cycle starts and ends with the battery idle at the same SoC reading, so the stored
              energy is the same at both ends and SoC calibration errors cancel; cycles with an unexplained SoC jump
              (BMS re-calibration) are dropped. Power up to {{ fmt(item.standby_w, 0) }} W (standby) is left out.
              Cycles are kept {{ fmt(item.history_days, 0) }} days. The plan uses the learned value with
              <code>efficiency: learned</code> on the battery once {{ item.min_cycles }} cycles have charged
              1.5 × the capacity; the savings accounting always uses the configured values.
            </div>
          </div>
        </template>

        <template v-else-if="isThermal">
          <div class="kpis">
            <div class="card">
              <div class="k">Idle (drift)</div>
              <div class="v">{{ rate(item.rates.drift_per_h) }}</div>
              <div class="savings-sub">configured {{ rate(item.configured.drift_per_h) }}</div>
            </div>
            <div class="card" v-for="m in thermalModes" :key="m">
              <div class="k">{{ modeName(m) }} (net)</div>
              <div class="v">{{ rate(item.rates.running_per_h[m]) }}</div>
              <div class="savings-sub" v-if="item.configured.running_per_h[m] != null">
                configured {{ rate(item.configured.running_per_h[m]) }}</div>
            </div>
            <div class="card">
              <div class="k">Typical error</div>
              <div class="v">{{ model.rmse == null ? '—' : fmt(model.rmse, 3) + ' ' + tUnit + '/h' }}</div>
              <div class="savings-sub">per 15-min step</div>
            </div>
            <div class="card">
              <div class="k">Windows used</div>
              <div class="v">{{ model.windows || '—' }}</div>
              <div class="savings-sub" v-if="model.windows">
                {{ model.off_windows }} idle · {{ Object.values(model.on_windows ?? {}).reduce((a, b) => a + b, 0) }} running · {{ model.outliers }} outliers</div>
            </div>
            <div class="card learn-wide">
              <div class="k">Form</div>
              <div class="v learn-formula">{{ thermalForm }}</div>
            </div>
          </div>
          <div class="panel full">
            <h2>Measured and model — {{ item.label }}</h2>
            <PlotlyChart :traces="thermalTraces" :layout="thermalLayout" height="340px" />
            <div class="footnote">
              The model line runs on its own from the measured value, restarted every 6 hours, so it
              shows how far the learned rates drift from reality. Shaded: share of each 15 minutes the
              device ran. Dashed lines: thresholds {{ item.bottom_threshold }}–{{ item.top_threshold }} {{ tUnit }}.
              Learned from <code>{{ item.entities.value }}</code>
              and <code>{{ Object.values(item.entities.modes).join(', ') }}</code>
              <template v-if="item.entities.ambient"> with ambient <code>{{ item.entities.ambient }}</code></template>.
              The planner uses these rates; until a model is learned it uses the configured ones.
            </div>
          </div>
        </template>

        <template v-else>
        <div v-if="model" class="kpis">
          <div class="card learn-wide">
            <div class="k">Daily energy</div>
            <div class="v learn-formula">{{ formula }}</div>
          </div>
          <div class="card">
            <div class="k">Typical error</div>
            <div class="v">{{ fmt(model.mae_kwh) }} kWh/day</div>
          </div>
          <div class="card">
            <div class="k">Variance explained</div>
            <div class="v">{{ fmt(model.r2 * 100, 0) }} %</div>
          </div>
          <div class="card">
            <div class="k">Days used</div>
            <div class="v">{{ model.n_days }}</div>
          </div>
          <div class="card">
            <div class="k">Excluded / outliers</div>
            <div class="v">{{ counts.excluded }} / {{ counts.outlier }}</div>
          </div>
          <div class="card">
            <div class="k">Carry-forward</div>
            <div class="v">{{ fmt(model.rho * 100, 0) }} %</div>
          </div>
        </div>

        <div class="row2">
          <div class="panel" v-if="isTemp">
            <h2>Daily energy against outdoor temperature</h2>
            <PlotlyChart :traces="scatterTraces"
              :layout="{ xaxis: { title: 'Daily mean outdoor temperature (°C)' }, yaxis: { title: 'kWh/day', rangemode: 'tozero' }, hovermode: 'closest' }"
              height="340px" />
          </div>
          <div class="panel">
            <h2>Hourly shape — share of the day's energy</h2>
            <PlotlyChart :traces="shapeTraces"
              :layout="{ xaxis: { title: 'Local hour', dtick: 3 }, yaxis: { title: '% of daily energy', rangemode: 'tozero' } }"
              height="340px" />
          </div>
        </div>

        <div class="panel full">
          <div class="learn-head" style="margin-bottom:4px">
            <h2 style="margin-bottom:0">Actual and model, per day</h2>
            <span class="learn-spacer"></span>
            <div class="range-btns" style="margin-bottom:0">
              <button v-for="r in RANGES" :key="r.days"
                :class="['range-btn', rangeDays === r.days ? 'active' : '']"
                @click="rangeDays = r.days">{{ r.label }}</button>
            </div>
          </div>
          <PlotlyChart :traces="seriesTraces"
            :layout="{ xaxis: { type: 'date', tickformat: '%d.%m.%y' }, yaxis: { title: 'kWh/day', rangemode: 'tozero' } }"
            height="300px" />
          <div class="footnote">
            The model line is the fitted curve for each day's measured temperature, without the
            carry-forward correction. Excluded and outlier days are not trained on, nor are days
            without a temperature reading.
          </div>
        </div>

        <div class="row2">
          <div class="panel">
            <h2>Forecast for the planning horizon</h2>
            <PlotlyChart :traces="forecastTraces"
              :layout="{ xaxis: { type: 'date', tickformat: '%H' }, yaxis: { title: 'kW', rangemode: 'tozero' } }"
              height="260px" />
          </div>
          <div class="panel">
            <h2>Forecast per day</h2>
            <table>
              <thead><tr><th>Day</th><th v-if="isTemp">Expected temp.</th><th>Energy</th></tr></thead>
              <tbody>
                <tr v-for="d in forecastDays" :key="d.date">
                  <td>{{ d.date }}</td>
                  <td v-if="isTemp">{{ fmt(d.temp) }} °C</td>
                  <td>{{ fmt(d.kwh) }} kWh</td>
                </tr>
              </tbody>
            </table>
            <div class="footnote" v-if="model?.last_day">
              Includes {{ fmt(model.rho * 100, 0) }} % of the {{ fmt(model.last_residual) }} kWh
              model error on {{ model.last_day }}, fading day by day. The first and last day
              are only partly inside the horizon.
            </div>
            <div class="footnote">
              Trained on <code>{{ item.history_entity }}</code>
              <template v-if="item.subtract?.length"> minus
                <template v-for="(e, i) in item.subtract" :key="e"><code>{{ e }}</code><template v-if="i < item.subtract.length - 1">, </template></template>
                (consumers with their own forecast)</template>
              <template v-if="item.temperature_entity"> with <code>{{ item.temperature_entity }}</code></template>
              <template v-if="item.weather_entity">, forecast from <code>{{ item.weather_entity }}</code></template>.
            </div>
          </div>
        </div>

        <div class="panel full" v-if="excludedDays.length">
          <h2>Days not trained on ({{ excludedDays.length }})</h2>
          <div class="learn-scroll">
            <table>
              <thead><tr><th>Day</th><th>Reason</th><th>Measured</th><th>Model</th><th v-if="isTemp">Temp.</th></tr></thead>
              <tbody>
                <tr v-for="d in excludedDays" :key="d.date">
                  <td>{{ d.date }}</td>
                  <td>{{ d.status === 'excluded' ? 'Excluded by rule' : 'Outlier' }}</td>
                  <td>{{ fmt(d.kwh) }} kWh</td>
                  <td>{{ d.model_kwh == null ? '—' : fmt(d.model_kwh) + ' kWh' }}</td>
                  <td v-if="isTemp">{{ d.temp == null ? '—' : fmt(d.temp) + ' °C' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        </template>
      </template>
    </div>
  `,
})

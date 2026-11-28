<script>
import { defineComponent, ref, computed } from 'vue'
import { refitLearned } from '../api.js'
import PlotlyChart from './PlotlyChart.js'
import { learnedStatus } from '../lib/learned'

// Everything learned about one device (one entry of /api/learned), ordered
// from what is useful every day to how the model works.

// One colour per thing, the same in every chart.
const C = {
  actual:   '#2a78d6',  // measured consumption
  model:    '#eb6834',  // what the model says
  outlier:  '#1baf7a',  // days the fit dropped on its own
  excluded: '#8b93a7',  // days removed by exclude / exclude_when
  active:   '#6250d6',  // hourly shape on days the temperature term is active
  idle:     '#008300',  // hourly shape on the other days
  running:  'rgba(42,120,214,.16)',  // thermal: share of a window the device ran
  ambient:  '#8b93a7',  // thermal: ambient input
  limit:    '#b8574a',  // thermal: thresholds
  power:    'rgba(42,120,214,.30)',  // efficiency: battery power
  window:   'rgba(235,104,52,.10)',  // efficiency: balance windows
  windowAlt: 'rgba(235,104,52,.18)',
  configured: '#8b93a7',             // efficiency: configured value
}

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
  name: 'LearnedModel',
  components: { PlotlyChart },
  props: {
    item: { type: Object, required: true },   // one entry of /api/learned
  },
  emits: ['refresh'],

  setup(props, { emit }) {
    const item = computed(() => props.item)
    const error = ref('')
    const busy = ref(false)
    const rangeDays = ref(90)
    const model = computed(() => item.value?.model ?? null)
    const isTemp = computed(() => model.value?.mode === 'temperature')
    const isThermal = computed(() => item.value?.kind === 'thermal')
    const isEfficiency = computed(() => item.value?.kind === 'efficiency')
    const isPv = computed(() => item.value?.kind === 'pv')

    async function refit() {
      if (busy.value) return
      busy.value = true
      try {
        await refitLearned(item.value.id)
        error.value = ''
        emit('refresh')
      } catch (e) {
        error.value = String(e.message ?? e)
      } finally {
        busy.value = false
      }
    }

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

    const statusPill = computed(() => learnedStatus(item.value))

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
      const trained = days.filter(d => d.status === 'used' || d.status === 'no_temp' || d.status === 'suspect')
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
        mark('suspect', 'Snow or fog on the panels', { color: C.excluded, size: 7, symbol: 'x-thin-open', line: { width: 1.5 } }),
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
    const bal = computed(() => item.value?.balance ?? null)
    // Each window with what the fitted model leaves unexplained (kWh).
    const windowsNewestFirst = computed(() => {
      const b = bal.value
      return [...(item.value?.windows ?? [])].reverse().map(w => {
        const hours = (new Date(w.end).getTime() - new Date(w.start).getTime()) / 3600000
        const resid = b
          ? b.efficiency * w.energy_in_kwh - w.energy_out_kwh / b.efficiency
            - b.self_consumption_w / 1000 * hours - (w.delta_stored_kwh ?? 0)
          : null
        return { ...w, hours, resid }
      })
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
      const shapes = (item.value?.windows ?? [])
        .filter(w => new Date(w.end).getTime() >= first)
        .map((w, i) => ({ type: 'rect', xref: 'x', yref: 'paper', y0: 0, y1: 1,
                          x0: toPlotlyMinute(w.start), x1: toPlotlyMinute(w.end),
                          fillcolor: i % 2 ? C.window : C.windowAlt, line: { width: 0 }, layer: 'below' }))
      // Symmetric power axis: its zero sits at 50 % SoC.
      const kw = Math.max(0.5, ...rows.map(r => Math.abs(r.power_w) / 1000)) * 1.05
      return {
        xaxis: { type: 'date', tickformat: '%d.%m. %H:%M' },
        yaxis: { title: 'SoC (%)', range: [0, 100] },
        yaxis2: { overlaying: 'y', side: 'right', showgrid: false, zeroline: true, range: [-kw, kw], ticksuffix: ' kW' },
        shapes, bargap: 0, margin: { t: 10, r: 55, b: 60, l: 50 },
      }
    })

    // ── PV: learned from the measured production ────────────────────────
    const pvForecastTraces = computed(() => {
      const pts = item.value?.forecast?.points ?? []
      if (!pts.length) return []
      const x = pts.map(p => toPlotlyDate(p.t))
      const traces = [
        {
          name: 'Cloud cover', type: 'bar', yaxis: 'y2', marker: { color: 'rgba(141,152,145,.18)' },
          x, y: pts.map(p => p.cloud), hovertemplate: '%{x|%a %H:%M} · %{y:.0f} %<extra>Cloud cover</extra>',
        },
        {
          name: 'Forecast', type: 'scatter', mode: 'lines', line: { color: C.model, width: 2, shape: 'hv' },
          x, y: pts.map(p => p.kw), hovertemplate: '%{x|%a %H:%M} · %{y:.2f} kW<extra>Forecast</extra>',
        },
      ]
      if (pts.some(p => Math.abs(p.kw - p.model_kw) > 0.01)) {
        traces.push({
          name: 'Before the correction', type: 'scatter', mode: 'lines',
          line: { color: C.model, width: 1.5, dash: 'dot', shape: 'hv' },
          x, y: pts.map(p => p.model_kw), hovertemplate: '%{x|%a %H:%M} · %{y:.2f} kW<extra>Before the correction</extra>',
        })
      }
      return traces
    })
    const azimuthText = az => (az == null ? '—' : az === 0 ? 'south'
      : `${fmt(Math.abs(az), 0)}° ${az < 0 ? 'east' : 'west'} of south`)
    const shadedBins = computed(() => Object.values(model.value?.shading ?? {}).filter(v => v < 0.9).length)

    const excludedDays = computed(() =>
      (item.value?.days ?? []).filter(d => d.status === 'excluded' || d.status === 'outlier').reverse())

    return {
      item, model, isTemp, isThermal, isEfficiency, isPv, error, busy, refit,
      pvForecastTraces, azimuthText, shadedBins,
      pct, bal, windowsNewestFirst, socTraces, socLayout,
      tUnit, rate, thermalModes, modeName, thermalForm, thermalTraces, thermalLayout,
      counts, formula, fittedAt, statusPill, fmt,
      scatterTraces, shapeTraces, seriesTraces, forecastTraces, forecastDays, excludedDays,
      rangeDays, RANGES,
    }
  },
})
</script>

<template>
  <div class="learned" data-testid="learned-model">
    <div class="learn-head">
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

    <!-- ── battery: efficiency and self-consumption ── -->
    <template v-if="isEfficiency">
      <div class="kpis">
        <div class="card">
          <div class="k">Efficiency, learned</div>
          <div class="v">{{ pct(bal?.efficiency) }}</div>
          <div class="savings-sub" v-if="bal">per direction · {{ pct(bal.round_trip) }} round trip
            · configured {{ pct(item.configured.round_trip) }}</div>
        </div>
        <div class="card">
          <div class="k">Self-consumption, learned</div>
          <div class="v">{{ bal ? fmt(bal.self_consumption_w, 0) + ' W' : '—' }}</div>
          <div class="savings-sub">configured {{ fmt(item.configured.self_consumption_w ?? 0, 0) }} W</div>
        </div>
        <div class="card">
          <div class="k">Plan uses</div>
          <div class="v">{{ pct(item.planned.charge_efficiency) }}</div>
          <div class="savings-sub">
            {{ pct(item.planned.charge_efficiency, 0) }} charge · {{ pct(item.planned.discharge_efficiency, 0) }} discharge
            · {{ fmt(item.planned.self_consumption_w ?? 0, 0) }} W · {{ item.status === 'learned' ? 'learned' : 'configured' }}
          </div>
        </div>
      </div>

      <div class="subpanel">
        <h3>State of charge and power</h3>
        <PlotlyChart :traces="socTraces" :layout="socLayout" height="320px" />
        <div class="footnote">Shaded: the rest-to-rest windows the values are learned from.</div>
      </div>

      <details class="learn-details">
        <summary>How it is learned</summary>
        <div class="kpis" v-if="bal">
          <div class="card">
            <div class="k">Windows</div>
            <div class="v">{{ bal.windows }}</div>
            <div class="savings-sub">
              {{ bal.soc_windows ?? bal.windows }} rest to rest<span v-if="bal.energy_windows"> · {{ bal.energy_windows }} of a month (power only)</span>
              · over {{ fmt(bal.days) }} days · {{ bal.confident ? 'confident' : 'collecting' }}
            </div>
          </div>
          <div class="card">
            <div class="k">Charged in them</div>
            <div class="v">{{ fmt(bal.energy_in_kwh) }} kWh</div>
            <div class="savings-sub">needs {{ fmt(item.capacity_kwh * 1.5) }} kWh</div>
          </div>
          <div class="card">
            <div class="k">Unexplained</div>
            <div class="v">± {{ fmt(bal.rms_error_kwh, 2) }} kWh</div>
            <div class="savings-sub">per window · at most {{ fmt(item.capacity_kwh * 0.03, 2) }} kWh</div>
          </div>
        </div>
        <div class="subpanel">
          <h3>Windows ({{ windowsNewestFirst.length }})</h3>
          <div class="learn-scroll">
            <table>
              <thead><tr><th>From</th><th>To</th><th>Hours</th><th>In</th><th>Out</th><th>Stored Δ</th><th>Unexplained</th></tr></thead>
              <tbody>
                <tr v-for="w in windowsNewestFirst" :key="w.start">
                  <td>{{ new Date(w.start).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) }}</td>
                  <td>{{ new Date(w.end).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) }}</td>
                  <td>{{ fmt(w.hours, 1) }}</td>
                  <td>{{ fmt(w.energy_in_kwh, 2) }}</td>
                  <td>{{ fmt(w.energy_out_kwh, 2) }}</td>
                  <td>{{ w.soc_known === false ? '—' : fmt(w.delta_stored_kwh, 2) }}</td>
                  <td>{{ w.resid == null ? '—' : fmt(w.resid, 2) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="footnote" v-if="!windowsNewestFirst.length">
            No window yet: the battery has to rest (idle for 10 minutes) and rest again at least 12 hours later.
          </div>
        </div>
        <div class="footnote">
          A window runs from the battery resting to resting again at least 12 hours later. The energy charged
          and discharged in it (measured) must explain the change of the stored energy (from the two SoC
          readings): η × in − out ÷ η − self-consumption × hours. Power history without SoC (imported from
          Home Assistant) counts in windows of a month: the change of the stored energy is unknown there, but
          it cannot exceed the capacity, small against a month's throughput. A weighted least-squares fit finds
          the efficiency η (per direction) and the self-consumption; a battery whose SoC windows do not fit is
          learned from its month windows alone. Windows with an unexplained SoC jump (BMS re-calibration) are
          dropped, except from full to full. Power up to {{ fmt(item.standby_w, 0) }} W while charging is the
          battery's standby and left out. With <code>efficiency: learned</code> on the battery, the plan and the
          savings accounting use the learned values once 6 windows have charged 1.5 × the capacity and the model
          explains them within their uncertainty.
        </div>
      </details>
    </template>

    <!-- ── heating and cooling: drift and running rates ── -->
    <template v-else-if="isThermal">
      <div class="subpanel">
        <h3>Measured and model</h3>
        <PlotlyChart :traces="thermalTraces" :layout="thermalLayout" height="340px" />
        <div class="footnote">
          Shaded: share of each 15 minutes the device ran. Dashed lines: thresholds
          {{ item.bottom_threshold }}–{{ item.top_threshold }} {{ tUnit }}.
        </div>
      </div>

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
      </div>

      <details class="learn-details">
        <summary>How the model works</summary>
        <div class="kpis">
          <div class="card learn-wide">
            <div class="k">Form</div>
            <div class="v learn-formula">{{ thermalForm }}</div>
          </div>
          <div class="card">
            <div class="k">Windows used</div>
            <div class="v">{{ model.windows || '—' }}</div>
            <div class="savings-sub" v-if="model.windows">
              {{ model.off_windows }} idle · {{ Object.values(model.on_windows ?? {}).reduce((a, b) => a + b, 0) }} running · {{ model.outliers }} outliers</div>
          </div>
        </div>
        <div class="footnote">
          The model line in the chart runs on its own from the measured value, restarted every 6 hours, so it
          shows how far the learned rates drift from reality.
          Learned from <code>{{ item.entities.value }}</code>
          and <code>{{ Object.values(item.entities.modes).join(', ') }}</code>
          <template v-if="item.entities.ambient"> with ambient <code>{{ item.entities.ambient }}</code></template>.
          The planner uses these rates; until a model is learned it uses the configured ones.
        </div>
      </details>
    </template>

    <!-- ── PV: learned from the measured production ── -->
    <template v-else-if="isPv">
      <div class="row2">
        <div class="subpanel">
          <h3>Forecast for the next days</h3>
          <PlotlyChart :traces="pvForecastTraces"
            :layout="{ xaxis: { type: 'date', tickformat: '%H' }, yaxis: { title: 'kW', rangemode: 'tozero' },
                       yaxis2: { overlaying: 'y', side: 'right', range: [0, 100], showgrid: false, title: 'cloud %' },
                       legend: { orientation: 'h', y: -0.2 } }"
            height="260px" />
          <div v-if="item.intraday" class="footnote">
            Last hour: measured {{ fmt(item.intraday.measured_kw, 2) }} kW, model {{ fmt(item.intraday.model_kw, 2) }} kW.
            The coming hours are corrected by {{ item.intraday.delta_kw >= 0 ? '+' : '−' }}{{ fmt(Math.abs(item.intraday.delta_kw), 2) }} kW,
            fading within about three hours.
          </div>
        </div>
        <div class="subpanel">
          <h3>Forecast per day</h3>
          <table>
            <thead><tr><th>Day</th><th>Energy</th></tr></thead>
            <tbody>
              <tr v-for="d in forecastDays" :key="d.date">
                <td>{{ d.date }}</td>
                <td>{{ fmt(d.kwh) }} kWh</td>
              </tr>
            </tbody>
          </table>
          <div class="footnote">The first and last day are only partly inside the planning horizon.</div>
        </div>
      </div>

      <div class="subpanel">
        <div class="panel-head">
          <h3>Measured and model, per day</h3>
          <span class="learn-spacer"></span>
          <div class="range-btns">
            <button v-for="r in RANGES" :key="r.days"
              :class="['range-btn', rangeDays === r.days ? 'active' : '']"
              @click="rangeDays = r.days">{{ r.label }}</button>
          </div>
        </div>
        <PlotlyChart :traces="seriesTraces"
          :layout="{ xaxis: { type: 'date', tickformat: '%d.%m.%y' }, yaxis: { title: 'kWh/day', rangemode: 'tozero' } }"
          height="300px" />
        <div class="footnote">
          The model line is what the archived weather forecast of each day gave, without the correction by the
          last hour. Most of the difference is the weather forecast itself.
        </div>
      </div>

      <div v-if="model" class="subpanel">
        <h3>What it learned about the PV system</h3>
        <div class="kpis" style="margin-bottom:0">
          <div class="card">
            <div class="k">Orientation</div>
            <div class="v">{{ azimuthText(model.azimuth) }}</div>
          </div>
          <div class="card">
            <div class="k">Tilt</div>
            <div class="v">{{ fmt(model.tilt, 0) }}°</div>
          </div>
          <div class="card">
            <div class="k">Power at 1000 W/m²</div>
            <div class="v">{{ fmt(model.k * 1000, 1) }} kW</div>
          </div>
          <div class="card">
            <div class="k">Inverter limit</div>
            <div class="v">{{ fmt(model.cap_kw, 1) }} kW</div>
          </div>
          <div class="card">
            <div class="k">Typical error</div>
            <div class="v">{{ fmt(model.mae_kwh) }} kWh/day</div>
            <div class="savings-sub">of {{ fmt(model.mean_kwh) }} kWh/day, last 60 days</div>
          </div>
          <div class="card">
            <div class="k">Days used</div>
            <div class="v">{{ model.n_days }}</div>
          </div>
        </div>
      </div>

      <details class="learn-details">
        <summary>How the model works</summary>
        <div v-if="model" class="kpis">
          <div class="card">
            <div class="k">Shaded sun positions</div>
            <div class="v">{{ shadedBins }}</div>
          </div>
          <div class="card">
            <div class="k">With snow on the ground</div>
            <div class="v">{{ fmt(model.snow_factor * 100, 0) }} %</div>
            <div class="savings-sub">{{ model.n_snow_hours }} hours seen</div>
          </div>
          <div class="card">
            <div class="k">Snow or fog on the panels</div>
            <div class="v">{{ model.suspect_days.length }} days</div>
          </div>
        </div>
        <div class="footnote">
          Hour by hour, the measured production of <code>{{ item.history_entity }}</code> is compared with
          what Open-Meteo's forecast for that hour said (direct and diffuse irradiance). From that the model
          learns the orientation, tilt and power of the panels, the inverter limit, a shading factor per sun
          position (what the best clear hours reach there), how far the weather forecast is off per type of sky,
          and how much the panels deliver while snow lies. Hours with snow and days that were forecast clear but
          stayed dark on cold days (snow or fog on the panels) are left out of the training. The forecast applies
          this to Open-Meteo's current forecast and corrects the next hours by the deviation of the last hour.
          Refitted every {{ fmt(item.refit_interval_h, 0) }} h.
        </div>
      </details>
    </template>

    <!-- ── consumption forecast ── -->
    <template v-else>
      <div class="row2">
        <div class="subpanel">
          <h3>Forecast for the next days</h3>
          <PlotlyChart :traces="forecastTraces"
            :layout="{ xaxis: { type: 'date', tickformat: '%H' }, yaxis: { title: 'kW', rangemode: 'tozero' } }"
            height="260px" />
        </div>
        <div class="subpanel">
          <h3>Forecast per day</h3>
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
          <div class="footnote">The first and last day are only partly inside the planning horizon.</div>
        </div>
      </div>

      <div class="subpanel">
        <div class="panel-head">
          <h3>Measured and forecast, per day</h3>
          <span class="learn-spacer"></span>
          <div class="range-btns">
            <button v-for="r in RANGES" :key="r.days"
              :class="['range-btn', rangeDays === r.days ? 'active' : '']"
              @click="rangeDays = r.days">{{ r.label }}</button>
          </div>
        </div>
        <PlotlyChart :traces="seriesTraces"
          :layout="{ xaxis: { type: 'date', tickformat: '%d.%m.%y' }, yaxis: { title: 'kWh/day', rangemode: 'tozero' } }"
          height="300px" />
      </div>

      <div class="row2">
        <div class="subpanel">
          <h3>A typical day: share of the day's energy per hour</h3>
          <PlotlyChart :traces="shapeTraces"
            :layout="{ xaxis: { title: 'Local hour', dtick: 3 }, yaxis: { title: '% of daily energy', rangemode: 'tozero' } }"
            height="300px" />
        </div>
        <div v-if="model" class="subpanel">
          <h3>How accurate it is</h3>
          <div class="kpis" style="margin-bottom:0">
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
          </div>
        </div>
      </div>

      <details class="learn-details">
        <summary>How the model works</summary>
        <div v-if="model" class="kpis">
          <div class="card learn-wide">
            <div class="k">Daily energy</div>
            <div class="v learn-formula">{{ formula }}</div>
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
        <div class="subpanel" v-if="isTemp">
          <h3>Daily energy against outdoor temperature</h3>
          <PlotlyChart :traces="scatterTraces"
            :layout="{ xaxis: { title: 'Daily mean outdoor temperature (°C)' }, yaxis: { title: 'kWh/day', rangemode: 'tozero' }, hovermode: 'closest' }"
            height="340px" />
        </div>
        <div class="footnote">
          The forecast line per day is the fitted curve for each day's measured temperature, without the
          carry-forward correction. Excluded and outlier days are not trained on, nor are days
          without a temperature reading.
          <template v-if="model?.last_day">
            The forecast includes {{ fmt(model.rho * 100, 0) }} % of the {{ fmt(model.last_residual) }} kWh
            model error on {{ model.last_day }}, fading day by day.
          </template>
          Trained on <code>{{ item.history_entity }}</code>
          <template v-if="item.subtract?.length"> minus
            <template v-for="(e, i) in item.subtract" :key="e"><code>{{ e }}</code><template v-if="i < item.subtract.length - 1">, </template></template>
            (consumers with their own forecast)</template>
          <template v-if="item.temperature_entity"> with <code>{{ item.temperature_entity }}</code></template>
          <template v-if="item.weather_entity">, forecast from <code>{{ item.weather_entity }}</code></template>.
        </div>
        <div class="subpanel" v-if="excludedDays.length">
          <h3>Days not trained on ({{ excludedDays.length }})</h3>
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
      </details>
    </template>
  </div>
</template>

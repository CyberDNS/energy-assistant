import { defineComponent, ref, computed, onMounted } from 'vue'
import { fetchSavings } from '../api.js'
import PlotlyChart from '../components/PlotlyChart.js'

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

export default defineComponent({
  name: 'SavingsView',
  components: { PlotlyChart },

  setup() {
    const range  = ref(RANGES[2])
    const data   = ref(null)
    const loaded = ref(false)
    const error  = ref(null)

    async function load(r) {
      range.value = r
      try {
        data.value = await fetchSavings(r.hours)
        error.value = null
      } catch (e) {
        error.value = String(e.message ?? e)
      }
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

    const kw = (kwhVal, hours) => (kwhVal == null || !hours ? null : kwhVal / hours)
    const storedText = house => (slots.value ?? []).map(r => {
      const d = r.stored_detail?.[house] ?? {}
      return Object.entries(d).map(([k, v]) => `${k}: ${v.toFixed(2)} kWh`).join('<br>')
    })

    const detailTraces = computed(() => {
      const r = slots.value ?? []
      const xs = slotX.value
      const line = (extra) => ({ mode: 'lines', x: xs, line: { shape: 'hv', width: 2 }, ...extra })
      const traces = [
        line({ name: 'Stored — real', y: r.map(s => s.real_stored_kwh),
          line: { shape: 'hv', width: 2.5, color: C_REAL }, customdata: storedText('real'),
          hovertemplate: '%{x|%H:%M}  %{y:.2f} kWh<br>%{customdata}<extra>Stored — real</extra>' }),
        line({ name: 'Stored — uncontrolled', y: r.map(s => s.uncontrolled_stored_kwh),
          line: { shape: 'hv', width: 2, color: C_UNCTRL, dash: 'dash' },
          customdata: storedText('uncontrolled'),
          hovertemplate: '%{x|%H:%M}  %{y:.2f} kWh<br>%{customdata}<extra>Stored — uncontrolled</extra>' }),
        line({ name: 'Import price', y: r.map(s => s.import_price), yaxis: 'y2',
          line: { shape: 'hv', width: 1.2, color: C_PRICE, dash: 'dot' },
          hovertemplate: '%{x|%H:%M}  %{y:.3f} €/kWh<extra>Import price</extra>' }),
        line({ name: 'Battery — real', y: r.map(s => kw(s.real_battery_kwh, s.hours)), yaxis: 'y3',
          line: { shape: 'hv', width: 2, color: C_REAL },
          hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra>Battery — real</extra>' }),
        line({ name: 'Battery — uncontrolled', y: r.map(s => kw(s.uncontrolled_battery_kwh, s.hours)),
          yaxis: 'y3', line: { shape: 'hv', width: 2, color: C_UNCTRL, dash: 'dash' },
          hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra>Battery — uncontrolled</extra>' }),
      ]
      if (r.some(s => (s.real_ev_kwh ?? 0) > 0 || (s.uncontrolled_ev_kwh ?? 0) > 0)) {
        traces.push(
          line({ name: 'Car — real', y: r.map(s => kw(s.real_ev_kwh, s.hours)), yaxis: 'y4',
            line: { shape: 'hv', width: 2, color: C_CAR },
            hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra>Car — real</extra>' }),
          line({ name: 'Car — uncontrolled', y: r.map(s => kw(s.uncontrolled_ev_kwh, s.hours)),
            yaxis: 'y4', line: { shape: 'hv', width: 2, color: C_CAR, dash: 'dash' },
            hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra>Car — uncontrolled</extra>' }),
        )
      }
      return traces
    })

    const cumulativeTraces = computed(() => [{
      name: 'Saving', mode: 'lines', x: x.value,
      y: cumulative(buckets.value.map(b => b.real)),
      line: { color: C_SAVING, width: 2.5, shape: 'hv' },
      fill: 'tozeroy', fillcolor: 'rgba(76,175,125,.12)', hovertemplate: HOVER.value,
    }])

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
    const cumulativeLayout = computed(() => layout({ showlegend: false }))
    const costLayout       = computed(() => layout())
    const detailLayout     = computed(() => {
      const base = layout()
      return {
        ...base,
        xaxis: { ...base.xaxis, anchor: 'y3' },
        yaxis:  { title: 'kWh stored', domain: [0.56, 1], rangemode: 'tozero' },
        yaxis2: { title: '€/kWh', overlaying: 'y', side: 'right', showgrid: false,
                  rangemode: 'tozero' },
        yaxis3: { title: 'battery kW (+ charge)', domain: [0, 0.44], zeroline: true,
                  zerolinecolor: '#9aa5a0' },
        // Cars draw ~10× the battery power: own scale, same panel.
        yaxis4: { title: 'car kW', overlaying: 'y3', side: 'right', showgrid: false,
                  rangemode: 'tozero', titlefont: { color: C_CAR }, tickfont: { color: C_CAR } },
        legend: { orientation: 'h', y: -0.14, font: { size: 10 } },
        margin: { t: 10, r: 60, b: 60, l: 50 },
      }
    })

    onMounted(() => load(range.value))

    return {
      RANGES, range, data, loaded, error, load, eur, kwh,
      buckets, totals, allTime, pct, stored, bucketLabel,
      savingTraces, cumulativeTraces, costTraces, detailTraces, slots, savingTitle,
      savingLayout, cumulativeLayout, costLayout, detailLayout,
    }
  },

  template: `
    <div>
      <div class="range-btns">
        <button v-for="r in RANGES" :key="r.hours"
          :class="['range-btn', range.hours === r.hours ? 'active' : '']"
          @click="load(r)">{{ r.label }}</button>
      </div>

      <div v-if="error" class="bad" style="margin-bottom:8px;font-size:.82rem">⚠ {{ error }}</div>

      <div v-if="loaded && !buckets.length" class="panel savings-empty">
        <h2>No savings recorded in this window</h2>
        <p>
          Savings are booked per 15-minute slot by comparing the real house with a
          simulated uncontrolled house on the same load, PV and prices. The first
          numbers appear a few minutes after start.
        </p>
      </div>

      <template v-else-if="totals">
        <div class="kpis savings-kpis">
          <div class="card">
            <div class="k">Saving</div>
            <div class="v" :class="totals.real >= 0 ? 'ok' : 'bad'">{{ eur(totals.real) }}</div>
            <div class="savings-sub" v-if="pct != null">{{ pct.toFixed(1) }} % of uncontrolled cost</div>
          </div>
          <div class="card">
            <div class="k">Uncontrolled house</div>
            <div class="v">{{ eur(totals.uncontrolled_cost) }}</div>
            <div class="savings-sub">grid bill {{ eur(totals.uncontrolled_bill) }}</div>
          </div>
          <div class="card">
            <div class="k">Real house</div>
            <div class="v">{{ eur(totals.real_cost) }}</div>
            <div class="savings-sub">grid bill {{ eur(totals.real_bill) }}</div>
          </div>
          <div class="card">
            <div class="k">Of which cars</div>
            <div class="v" :class="totals.ev_saving >= 0 ? 'ok' : 'bad'">{{ eur(totals.ev_saving) }}</div>
            <div class="savings-sub">smart vs. last-moment charging</div>
          </div>
          <div class="card">
            <div class="k">Still stored</div>
            <div class="v">{{ kwh(stored.real.kwh) }}</div>
            <div class="savings-sub">uncontrolled {{ kwh(stored.uncontrolled.kwh) }}</div>
          </div>
          <div class="card" v-if="allTime">
            <div class="k">All time</div>
            <div class="v" :class="allTime.real >= 0 ? 'ok' : 'bad'">{{ eur(allTime.real) }}</div>
            <div class="savings-sub">over {{ (allTime.hours / 24).toFixed(1) }} days</div>
          </div>
        </div>

        <div class="panel" style="margin-bottom:10px">
          <h2>Saving per {{ savingTitle }}</h2>
          <PlotlyChart :traces="savingTraces" :layout="savingLayout" height="280px" />
        </div>

        <div class="panel" style="margin-bottom:10px" v-if="slots && slots.length">
          <h2>Batteries and cars — real vs. uncontrolled</h2>
          <PlotlyChart :traces="detailTraces" :layout="detailLayout" height="440px" />
        </div>

        <div class="row2-eq">
          <div class="panel">
            <h2>Cumulative saving</h2>
            <PlotlyChart :traces="cumulativeTraces" :layout="cumulativeLayout" height="260px" />
          </div>
          <div class="panel">
            <h2>Cost per {{ bucketLabel }} — uncontrolled vs. real</h2>
            <PlotlyChart :traces="costTraces" :layout="costLayout" barmode="group" height="260px" />
          </div>
        </div>

        <p class="savings-note">
          Compared against a simulated uncontrolled house on the same load, PV and prices:
          its batteries charge from PV surplus only and cover the household load, and cars charge
          as late as possible to reach their charge plan. Stored energy is costed when it is used,
          at what it cost to store it (ledger) — charging saves nothing by itself, the saving shows
          when the energy replaces grid power.
        </p>
      </template>
    </div>
  `,
})

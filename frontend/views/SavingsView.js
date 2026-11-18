import { defineComponent, ref, computed, onMounted } from 'vue'
import { fetchSavings } from '../api.js'
import PlotlyChart from '../components/PlotlyChart.js'

// Predicted vs. real savings (see core/savings.py).  All values in €.

const C_PREDICTED = '#6b7bb5'
const C_REAL      = '#4caf7d'
const C_LAST_PLAN = '#ad7b00'
const C_LOSS      = '#e07070'

const RANGES = [
  { days: 7,   label: '7 d' },
  { days: 30,  label: '30 d' },
  { days: 90,  label: '90 d' },
  { days: 365, label: '1 y' },
]

function eur(v, signed = false) {
  if (v == null) return '—'
  const s = Math.abs(v).toFixed(2) + ' €'
  if (v < -0.005) return '−' + s
  return (signed && v > 0.005 ? '+' : '') + s
}

function sum(rows, key) {
  return rows.reduce((acc, r) => acc + (r[key] ?? 0), 0)
}

function cumulative(values) {
  let acc = 0
  return values.map(v => (acc += v))
}

const HOVER = '%{x|%d.%m.}  %{y:.2f} €<extra>%{fullData.name}</extra>'

const DAY_MS = 86400000

// One labelled tick per day (bars sit on midnight, so each label is under its
// own bar); coarser steps once the range would not fit.
function dayTicks(nDays) {
  if (nDays <= 31) return { dtick: DAY_MS, tick0: '2000-01-03' }
  if (nDays <= 120) return { dtick: 7 * DAY_MS, tick0: '2000-01-03' }  // Mondays
  return { dtick: 'M1', tick0: '2000-01-01' }
}

export default defineComponent({
  name: 'SavingsView',
  components: { PlotlyChart },

  setup() {
    const days    = ref(30)
    const rows    = ref([])
    const allTime = ref(null)
    const loaded  = ref(false)
    const error   = ref(null)

    async function load(d) {
      days.value = d
      try {
        const resp = await fetchSavings(d)
        rows.value = resp.days ?? []
        allTime.value = resp.totals ?? null
        error.value = null
      } catch (e) {
        error.value = String(e.message ?? e)
      }
      loaded.value = true
    }

    const period = computed(() => {
      const r = rows.value
      const real = sum(r, 'real')
      const predicted = sum(r, 'predicted')
      const baseline = sum(r, 'baseline_cost')
      return {
        real,
        predicted,
        deviation: real - predicted,
        predictedLatest: sum(r, 'predicted_latest'),
        baseline,
        actual: sum(r, 'actual_cost'),
        // Share of the unplanned bill that was saved.
        pct: baseline > 0.005 ? real / baseline * 100 : null,
        // How much of the promised saving actually arrived.
        realised: Math.abs(predicted) > 0.005 ? real / predicted * 100 : null,
      }
    })

    const x = computed(() => rows.value.map(r => r.date))

    const dailyTraces = computed(() => [
      { name: 'Predicted (day-ahead)', type: 'bar', x: x.value, y: rows.value.map(r => r.predicted),
        marker: { color: C_PREDICTED }, hovertemplate: HOVER },
      { name: 'Real', type: 'bar', x: x.value, y: rows.value.map(r => r.real),
        marker: { color: C_REAL }, hovertemplate: HOVER },
      { name: 'Predicted (last plan)', mode: 'lines+markers', x: x.value,
        y: rows.value.map(r => r.predicted_latest),
        line: { color: C_LAST_PLAN, width: 1.5, dash: 'dot' }, marker: { size: 5 }, hovertemplate: HOVER },
    ])

    const cumulativeTraces = computed(() => [
      { name: 'Predicted (day-ahead)', mode: 'lines', x: x.value,
        y: cumulative(rows.value.map(r => r.predicted)),
        line: { color: C_PREDICTED, dash: 'dash' }, hovertemplate: HOVER },
      { name: 'Real', mode: 'lines', x: x.value,
        y: cumulative(rows.value.map(r => r.real)),
        line: { color: C_REAL, width: 2.5 }, fill: 'tozeroy', fillcolor: 'rgba(76,175,125,.12)',
        hovertemplate: HOVER },
    ])

    const deviationTraces = computed(() => {
      const y = rows.value.map(r => r.deviation)
      return [{
        name: 'Real − predicted', type: 'bar', x: x.value, y,
        marker: { color: y.map(v => (v >= 0 ? C_REAL : C_LOSS)) }, hovertemplate: HOVER,
      }]
    })

    // One layout object per chart: Plotly writes the computed axis ranges
    // back into the layout it is given, so charts must not share axes.
    const layout = (extra = {}) => ({
      xaxis: {
        type: 'date', tickformat: '%d.%m.', tickangle: 0, tickmode: 'linear',
        ...dayTicks(rows.value.length),
      },
      yaxis: { title: '€', zeroline: true, zerolinecolor: '#9aa5a0' },
      ...extra,
    })
    const dailyLayout      = computed(() => layout())
    const cumulativeLayout = computed(() => layout())
    const deviationLayout  = computed(() => layout({ showlegend: false }))

    onMounted(() => load(days.value))

    return {
      RANGES, days, rows, allTime, loaded, error, load, period, eur,
      dailyTraces, cumulativeTraces, deviationTraces,
      dailyLayout, cumulativeLayout, deviationLayout,
    }
  },

  template: `
    <div>
      <div class="range-btns">
        <button v-for="r in RANGES" :key="r.days"
          :class="['range-btn', days === r.days ? 'active' : '']"
          @click="load(r.days)">{{ r.label }}</button>
      </div>

      <div v-if="error" class="bad" style="margin-bottom:8px;font-size:.82rem">⚠ {{ error }}</div>

      <div v-if="loaded && !rows.length" class="panel savings-empty">
        <h2>No savings recorded yet</h2>
        <p>
          Savings are booked per 15-minute slot once a slot has both a measurement
          and a day-ahead prediction. A day's prediction is frozen shortly before
          midnight, so the first numbers appear during the day after the first start.
        </p>
      </div>

      <template v-else-if="rows.length">
        <div class="kpis savings-kpis">
          <div class="card">
            <div class="k">Real savings</div>
            <div class="v" :class="period.real >= 0 ? 'ok' : 'bad'">{{ eur(period.real) }}</div>
            <div class="savings-sub" v-if="period.pct != null">{{ period.pct.toFixed(1) }} % of unplanned cost</div>
          </div>
          <div class="card">
            <div class="k">Predicted (day-ahead)</div>
            <div class="v">{{ eur(period.predicted) }}</div>
            <div class="savings-sub" v-if="period.realised != null">{{ period.realised.toFixed(0) }} % realised</div>
          </div>
          <div class="card">
            <div class="k">Deviation</div>
            <div class="v" :class="period.deviation >= 0 ? 'ok' : 'bad'">{{ eur(period.deviation, true) }}</div>
            <div class="savings-sub">real − predicted</div>
          </div>
          <div class="card">
            <div class="k">Predicted (last plan)</div>
            <div class="v">{{ eur(period.predictedLatest) }}</div>
            <div class="savings-sub">just before each slot</div>
          </div>
          <div class="card">
            <div class="k">Unplanned cost</div>
            <div class="v">{{ eur(period.baseline) }}</div>
            <div class="savings-sub">actual {{ eur(period.actual) }}</div>
          </div>
          <div class="card" v-if="allTime">
            <div class="k">All time</div>
            <div class="v" :class="allTime.real >= 0 ? 'ok' : 'bad'">{{ eur(allTime.real) }}</div>
            <div class="savings-sub">predicted {{ eur(allTime.predicted) }}</div>
          </div>
        </div>

        <div class="panel" style="margin-bottom:10px">
          <h2>Savings per day — predicted vs. real</h2>
          <PlotlyChart :traces="dailyTraces" :layout="dailyLayout" barmode="group" height="300px" />
        </div>

        <div class="row2-eq">
          <div class="panel">
            <h2>Cumulative savings</h2>
            <PlotlyChart :traces="cumulativeTraces" :layout="cumulativeLayout" height="260px" />
          </div>
          <div class="panel">
            <h2>Deviation per day (real − predicted)</h2>
            <PlotlyChart :traces="deviationTraces" :layout="deviationLayout" height="260px" />
          </div>
        </div>

        <p class="savings-note">
          Compared against an unplanned site: batteries in plain self-consumption, cars charging
          at full power when plugged in. Unplanned cost minus actual cost is not exactly the
          saving — energy still stored (or still owed to a car) is valued as well.
        </p>
      </template>
    </div>
  `,
})

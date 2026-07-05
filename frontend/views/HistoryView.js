import { defineComponent, ref, onMounted } from 'vue'
import { fetchHistory, fetchLedger } from '../api.js'
import PlotlyChart from '../components/PlotlyChart.js'

function localTs(isoArr) {
  return isoArr.map(t => new Date(t).toLocaleString())
}

export default defineComponent({
  name: 'HistoryView',
  components: { PlotlyChart },

  setup() {
    const hours = ref(6)
    const ranges = [6, 12, 24, 48, 168]

    const socTraces     = ref([])
    const pwrTraces     = ref([])
    const batPwrTraces  = ref([])
    const basisTraces   = ref([])

    async function load(h) {
      hours.value = h
      let resp, ledgerSnap = []
      try { resp = await fetchHistory(h) } catch { return }
      try { ledgerSnap = await fetchLedger() } catch { /* ignore */ }

      const hist    = resp.measurements ?? resp
      const ledHist = resp.ledger ?? {}

      // SoC history
      const soc = []
      for (const [did, rows] of Object.entries(hist)) {
        if (!rows.some(r => r.soc_pct != null)) continue
        soc.push({ name: did, mode: 'lines',
          x: localTs(rows.map(r => r.t)),
          y: rows.map(r => r.soc_pct),
          hovertemplate: '%{y:.1f}%' })
      }
      socTraces.value = soc

      // Power history (non-storage devices)
      const pwr = []
      for (const [did, rows] of Object.entries(hist)) {
        if (rows.some(r => r.soc_pct != null)) continue
        if (!rows.some(r => r.power_w != null)) continue
        pwr.push({ name: did, mode: 'lines',
          x: localTs(rows.filter(r => r.power_w != null).map(r => r.t)),
          y: rows.filter(r => r.power_w != null).map(r => r.power_w),
          hovertemplate: '%{y:.0f} W' })
      }
      pwrTraces.value = pwr

      // Battery power history (storage devices)
      const batPwr = []
      for (const [did, rows] of Object.entries(hist)) {
        if (!rows.some(r => r.soc_pct != null)) continue
        const x = localTs(rows.filter(r => r.power_w != null).map(r => r.t))
        const y = rows.filter(r => r.power_w != null).map(r => r.power_w)
        if (x.length) batPwr.push({ name: did, mode: 'lines', x, y, hovertemplate: '%{y:.0f} W' })
      }
      batPwrTraces.value = batPwr

      // Cost basis history
      const basis = []
      for (const [did, rows] of Object.entries(ledHist)) {
        if (!rows.length) continue
        basis.push({ name: did + ' basis', mode: 'lines',
          x: localTs(rows.map(r => r.t)),
          y: rows.map(r => r.cost_basis_eur_per_kwh),
          hovertemplate: '%{y:.4f} €/kWh' })
      }
      // Fallback: show current snapshot as horizontal reference
      if (!basis.length && ledgerSnap.length) {
        const now = new Date().toLocaleString()
        for (const s of ledgerSnap) {
          basis.push({ name: s.device_id + ' (current)', mode: 'markers',
            x: [now], y: [s.cost_basis_eur_per_kwh], hovertemplate: '%{y:.4f} €/kWh' })
        }
      }
      basisTraces.value = basis
    }

    onMounted(() => load(6))

    return { hours, ranges, load, socTraces, pwrTraces, batPwrTraces, basisTraces }
  },

  template: `
    <div>
      <div class="range-btns">
        <button v-for="h in ranges" :key="h"
          :class="['range-btn', hours === h ? 'active' : '']"
          @click="load(h)">
          {{ h === 168 ? '7 d' : h + ' h' }}
        </button>
      </div>
      <div class="row2-eq">
        <div class="panel">
          <h2>Battery SoC History</h2>
          <PlotlyChart :traces="socTraces" :layout="{ yaxis: { title: 'SoC %', range: [0, 105] } }" height="280px" />
        </div>
        <div class="panel">
          <h2>Power History</h2>
          <PlotlyChart :traces="pwrTraces" :layout="{ yaxis: { title: 'W' } }" height="280px" />
        </div>
      </div>
      <div class="row2-eq">
        <div class="panel">
          <h2>Battery Power History</h2>
          <PlotlyChart :traces="batPwrTraces" :layout="{ yaxis: { title: 'W', zeroline: true } }" height="240px" />
        </div>
        <div class="panel">
          <h2>Cost Basis History</h2>
          <PlotlyChart :traces="basisTraces" :layout="{ yaxis: { title: '€/kWh' } }" height="240px" />
        </div>
      </div>
    </div>
  `,
})

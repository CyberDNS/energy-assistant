import { defineComponent, ref, onMounted, onUnmounted } from 'vue'
import { fetchPlan, fetchForecast, fetchEv, triggerPlanRefresh } from '../api.js'
import PlotlyChart from '../components/PlotlyChart.js'
import EvCard from '../components/EvCard.js'

// Convert ISO UTC string to a local-time string Plotly can parse as a date
// e.g. "2026-07-03 14:30:00" — Plotly treats these as-is (no UTC conversion)
function toPlotlyDate(iso) {
  const d = new Date(iso)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:00`
}

function localTs(isoArr) {
  return isoArr.map(toPlotlyDate)
}

// X-axis config shared by all plan charts: compact ticks, no cluttered labels
const XAXIS = {
  type: 'date',
  tickformat: '%H:%M<br>%d.%m.',
  nticks: 10,
  tickangle: 0,
}

function buildFlowTraces(intents, forecast, evDeviceIds) {
  const tsMap = {}
  forecast.timestamps.forEach((t, i) => { tsMap[t] = i })

  const pvKw      = forecast.pv_kw
  const consKw    = forecast.consumption_kw
  const expPrices = forecast.export_prices
  // Use Tibber variable prices when available (15-min granularity);
  // fall back to blended tariff prices if not present.
  const varPrices = forecast.variable_prices?.length ? forecast.variable_prices : forecast.prices
  const varEst    = forecast.variable_price_is_estimated ?? []
  // Split into real (solid) and estimated/repeated (dashed) segments.
  const priceReal = varPrices.map((p, i) => varEst[i] ? null : p)
  const priceEst  = varPrices.map((p, i) => varEst[i] ? p  : null)
  const hasEst    = priceEst.some(p => p !== null)

  const chargeKw    = new Array(pvKw.length).fill(0)
  const dischargeKw = new Array(pvKw.length).fill(0)
  const evChargeKw  = new Array(pvKw.length).fill(0)

  for (const intent of intents) {
    const i = tsMap[intent.timestep]
    if (i == null) continue
    const kw = intent.planned_kw ?? 0
    if (evDeviceIds.has(intent.device_id)) {
      if (kw > 0) evChargeKw[i] += kw
    } else {
      if (kw > 0) chargeKw[i] += kw
      else if (kw < 0) dischargeKw[i] += Math.abs(kw)
    }
  }

  const ts = localTs(forecast.timestamps)

  const gridImport = pvKw.map((pv, i) =>
    Math.max(0, consKw[i] + chargeKw[i] + evChargeKw[i] - pv - dischargeKw[i])
  )
  const gridExport = pvKw.map((pv, i) =>
    Math.max(0, pv + dischargeKw[i] - consKw[i] - chargeKw[i] - evChargeKw[i])
  )

  return {
    flowTraces: [
      { name: 'PV',          type: 'bar', x: ts, y: pvKw,                    marker: { color: '#f0c040' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'Discharge',   type: 'bar', x: ts, y: dischargeKw,             marker: { color: '#4caf7d' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'Grid import', type: 'bar', x: ts, y: gridImport,              marker: { color: '#e07070' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'Consumption', type: 'bar', x: ts, y: consKw.map(v => -v),     marker: { color: '#6b7bb5' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'Charge',      type: 'bar', x: ts, y: chargeKw.map(v => -v),   marker: { color: '#3a9ad9' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'EV Charging', type: 'bar', x: ts, y: evChargeKw.map(v => -v), marker: { color: '#00acc1' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'Grid export', type: 'bar', x: ts, y: gridExport.map(v => -v), marker: { color: '#b07030' }, hovertemplate: '%{y:.2f} kW' },
    ],
    forecastTraces: [
      { name: 'PV forecast',    mode: 'lines', x: ts, y: pvKw,            line: { color: '#f0c040' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'Consumption',    mode: 'lines', x: ts, y: consKw,          line: { color: '#6b7bb5' }, hovertemplate: '%{y:.2f} kW' },
      { name: 'EV Charging',    mode: 'lines', x: ts, y: evChargeKw,      line: { color: '#00acc1', dash: 'dot' }, hovertemplate: '%{y:.2f} kW' },
    ],
    priceTraces: [
      { name: 'Import price',      mode: 'lines', x: ts, y: priceReal, line: { color: '#e07070' },               connectgaps: false, hovertemplate: '%{y:.4f} €/kWh' },
      { name: 'Import (forecast)', mode: 'lines', x: ts, y: priceEst,  line: { color: '#e07070', dash: 'dash' }, connectgaps: false, hovertemplate: '%{y:.4f} €/kWh', showlegend: hasEst },
      { name: 'Export price',      mode: 'lines', x: ts, y: expPrices, line: { color: '#4caf7d' },               hovertemplate: '%{y:.4f} €/kWh' },
    ],
    evChargeKw,
  }
}

function buildSocTraces(intents, forecast, evDeviceIds) {
  const storageDevices = [...new Set(
    intents.filter(i => !evDeviceIds.has(i.device_id) && i.stored_energy_kwh != null).map(i => i.device_id)
  )]
  const traces = []
  for (const did of storageDevices) {
    const caps = forecast.storage_capacity?.[did]
    if (!caps?.capacity_kwh) continue

    const devIntents = intents
      .filter(i => i.device_id === did && i.stored_energy_kwh != null)
      .sort((a, b) => a.timestep.localeCompare(b.timestep))

    const x = localTs(devIntents.map(i => i.timestep))
    const y = devIntents.map(i => i.stored_energy_kwh / caps.capacity_kwh * 100)

    if (x.length) {
      traces.push({ name: did, mode: 'lines', x, y, hovertemplate: '%{y:.1f}%' })
    }
  }
  return traces
}

export default defineComponent({
  name: 'PlanView',
  components: { PlotlyChart, EvCard },

  setup() {
    const evList      = ref([])
    const planMeta    = ref('')
    const flowTraces  = ref([])
    const fcastTraces = ref([])
    const priceTraces = ref([])
    const socTraces   = ref([])
    let timer = null

    const refreshing = ref(false)
    async function triggerRefresh() {
      refreshing.value = true
      try { await triggerPlanRefresh() } catch (e) { console.error(e) }
      await refresh()
      refreshing.value = false
    }

    async function refresh() {
      try {
        const [plan, forecast, evs] = await Promise.all([fetchPlan(), fetchForecast(), fetchEv()])

        evList.value = evs
        planMeta.value = plan.created_at
          ? `Plan created: ${new Date(plan.created_at).toLocaleString()} · step ${plan.step_minutes} min`
          : 'No plan yet'

        const evDeviceIds = new Set(evs.map(e => e.device_id))
        const { flowTraces: ft, forecastTraces: fct, priceTraces: pt } =
          buildFlowTraces(plan.intents, forecast, evDeviceIds)

        flowTraces.value  = ft
        fcastTraces.value = fct
        priceTraces.value = pt
        socTraces.value   = buildSocTraces(plan.intents, forecast, evDeviceIds)
      } catch (e) {
        console.error('PlanView refresh failed', e)
      }
    }

    onMounted(() => { refresh(); timer = setInterval(refresh, 60_000) })
    onUnmounted(() => clearInterval(timer))

    return { evList, planMeta, flowTraces, fcastTraces, priceTraces, socTraces, refresh, triggerRefresh, refreshing, XAXIS }
  },

  template: `
    <div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <p class="footnote" style="margin:0">{{ planMeta }}</p>
        <button class="btn btn-neutral" style="padding:4px 10px;font-size:.78rem" :disabled="refreshing" @click="triggerRefresh">
          {{ refreshing ? '…' : '↺ Refresh plan' }}
        </button>
      </div>

      <div v-if="evList.length" class="full panel" style="margin-bottom:10px">
        <h2>EV Charging</h2>
        <div class="ev-cards">
          <EvCard v-for="ev in evList" :key="ev.asset_id" :ev="ev" @refresh="refresh" />
        </div>
      </div>

      <div class="full panel">
        <h2>Energy Flow — Supply &amp; Demand</h2>
        <PlotlyChart :traces="flowTraces"
          :layout="{ yaxis: { title: 'kW' }, xaxis: XAXIS }"
          barmode="relative" height="280px" />
      </div>

      <div class="row2-eq">
        <div class="panel">
          <h2>PV &amp; Consumption Forecast</h2>
          <PlotlyChart :traces="fcastTraces"
            :layout="{ yaxis: { title: 'kW' }, xaxis: XAXIS }"
            height="220px" />
        </div>
        <div class="panel">
          <h2>Electricity Prices</h2>
          <PlotlyChart :traces="priceTraces"
            :layout="{ yaxis: { title: '€/kWh' }, xaxis: XAXIS }"
            height="220px" />
        </div>
      </div>

      <div class="full panel">
        <h2>Battery SoC Trajectory</h2>
        <PlotlyChart :traces="socTraces"
          :layout="{ yaxis: { title: 'SoC %', range: [0, 105] }, xaxis: XAXIS }"
          height="220px" />
      </div>
    </div>
  `,
})

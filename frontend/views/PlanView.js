import { defineComponent, ref, watch, onMounted, onUnmounted } from 'vue'
import { fetchPlan, fetchForecast, fetchEv, fetchControllable, triggerPlanRefresh } from '../api.js'
import { planVersion, planning } from '../composables/useStatus.js'
import PlotlyChart from '../components/PlotlyChart.js'
import EvCard from '../components/EvCard.js'
import ControllableDeviceCard from '../components/ControllableDeviceCard.js'

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

// X-axis: hour labels placed dynamically by PlotlyChart; minor ticks at each 15-min step
const XAXIS = {
  type: 'date',
  tickformat: '%H',
  tickangle: 0,
  minor: { dtick: 900000, ticks: 'outside', ticklen: 4 },
}

function buildFlowTraces(intents, forecast, evDeviceIds, planFlows) {
  const tsMap = {}
  forecast.timestamps.forEach((t, i) => { tsMap[t] = i })

  // Prefer the solver's effective PV series (includes the live-PV floor for
  // the current hour) — the raw forecast can be lower than what the plan was
  // actually solved with, which made derived grid import look impossible.
  const pvKw = [...forecast.pv_kw]
  const solvedImport = new Array(pvKw.length).fill(null)
  const solvedExport = new Array(pvKw.length).fill(null)
  for (const f of planFlows ?? []) {
    const i = tsMap[f.timestep]
    if (i == null) continue
    pvKw[i] = f.pv_kw
    solvedImport[i] = f.grid_import_kw
    solvedExport[i] = f.grid_export_kw
  }
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

  // Grid import/export: use the solver's own values when available (always
  // consistent with the plan); derive from the series only as fallback.
  const gridImport = pvKw.map((pv, i) =>
    solvedImport[i] ?? Math.max(0, consKw[i] + chargeKw[i] + evChargeKw[i] - pv - dischargeKw[i])
  )
  const gridExport = pvKw.map((pv, i) =>
    solvedExport[i] ?? Math.max(0, pv + dischargeKw[i] - consKw[i] - chargeKw[i] - evChargeKw[i])
  )

  return {
    flowTraces: [
      { name: 'PV',          type: 'bar', x: ts, y: pvKw,                    marker: { color: '#f0c040' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'Discharge',   type: 'bar', x: ts, y: dischargeKw,             marker: { color: '#4caf7d' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'Grid import', type: 'bar', x: ts, y: gridImport,              marker: { color: '#e07070' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'Consumption', type: 'bar', x: ts, y: consKw.map(v => -v),     marker: { color: '#6b7bb5' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'Charge',      type: 'bar', x: ts, y: chargeKw.map(v => -v),   marker: { color: '#3a9ad9' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'EV Charging', type: 'bar', x: ts, y: evChargeKw.map(v => -v), marker: { color: '#00acc1' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'Grid export', type: 'bar', x: ts, y: gridExport.map(v => -v), marker: { color: '#b07030' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
    ],
    forecastTraces: [
      { name: 'PV forecast',    mode: 'lines', x: ts, y: pvKw,            line: { color: '#f0c040' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'Consumption',    mode: 'lines', x: ts, y: consKw,          line: { color: '#6b7bb5' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
      { name: 'EV Charging',    mode: 'lines', x: ts, y: evChargeKw,      line: { color: '#00acc1', dash: 'dot' }, hovertemplate: '%{x|%H:%M}  %{y:.2f} kW<extra></extra>' },
    ],
    priceTraces: [
      { name: 'Import price',      mode: 'lines', x: ts, y: priceReal, line: { color: '#e07070' },               connectgaps: false, hovertemplate: '%{x|%H:%M}  %{y:.4f} €/kWh<extra></extra>' },
      { name: 'Import (forecast)', mode: 'lines', x: ts, y: priceEst,  line: { color: '#e07070', dash: 'dash' }, connectgaps: false, hovertemplate: '%{x|%H:%M}  %{y:.4f} €/kWh<extra></extra>', showlegend: hasEst },
      { name: 'Export price',      mode: 'lines', x: ts, y: expPrices, line: { color: '#4caf7d' },               hovertemplate: '%{x|%H:%M}  %{y:.4f} €/kWh<extra></extra>' },
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
      traces.push({ name: did, mode: 'lines', x, y, hovertemplate: '%{x|%H:%M}  %{y:.1f}%<extra></extra>' })
    }
  }
  return traces
}

export default defineComponent({
  name: 'PlanView',
  components: { PlotlyChart, EvCard, ControllableDeviceCard },

  setup() {
    const evList       = ref([])
    const controllable = ref([])
    const planMeta     = ref('')
    const flowTraces   = ref([])
    const fcastTraces  = ref([])
    const priceTraces  = ref([])
    const socTraces    = ref([])
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
        const [plan, forecast, evs, ctrl] = await Promise.all([
          fetchPlan(), fetchForecast(), fetchEv(), fetchControllable(),
        ])

        controllable.value = ctrl?.devices ?? []
        planMeta.value = plan.created_at
          ? `Plan created: ${new Date(plan.created_at).toLocaleString()} · step ${plan.step_minutes} min`
          : 'No plan yet'

        evList.value = evs
        const evDeviceIds = new Set(evs.map(e => e.device_id))
        const { flowTraces: ft, forecastTraces: fct, priceTraces: pt } =
          buildFlowTraces(plan.intents, forecast, evDeviceIds, plan.flows)

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
    // Refetch immediately when the server pushes a new plan (SSE 'plan'
    // event) — e.g. after '↺ Refresh plan' finishes or a scheduled re-solve.
    watch(planVersion, () => refresh())

    return { evList, controllable, planMeta, flowTraces, fcastTraces, priceTraces, socTraces, refresh, triggerRefresh, refreshing, planning, XAXIS }
  },

  template: `
    <div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
        <p class="footnote" style="margin:0">{{ planMeta }}</p>
        <button class="btn btn-neutral" style="padding:4px 10px;font-size:.78rem" :disabled="refreshing || planning" @click="triggerRefresh">
          {{ refreshing ? '…' : '↺ Refresh plan' }}
        </button>
        <transition name="fade">
          <span v-if="planning" class="plan-busy">
            <span class="spinner"></span> Recalculating plan…
          </span>
        </transition>
      </div>

      <div v-if="evList.length" class="full panel" style="margin-bottom:10px">
        <h2>EV Charging</h2>
        <div class="ev-cards">
          <EvCard v-for="ev in evList" :key="ev.asset_id" :ev="ev" @refresh="refresh" />
        </div>
      </div>

      <div v-if="controllable.length" class="full panel" style="margin-bottom:10px">
        <h2>Controllable Devices</h2>
        <div class="ctrl-list">
          <ControllableDeviceCard v-for="d in controllable" :key="d.device_id" :device="d" />
        </div>
      </div>

      <div class="full panel">
        <h2>Energy Flow — Supply &amp; Demand</h2>
        <PlotlyChart :traces="flowTraces"
          :layout="{ yaxis: { title: 'kW' }, xaxis: XAXIS, bargap: 0.05 }"
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

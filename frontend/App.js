import { defineComponent, computed, provide } from 'vue'
import { RouterView, RouterLink } from 'vue-router'
import { useStatus } from '/ui/composables/useStatus.js'

function fmt(v, dec = 0) {
  return v == null ? '—' : Number(v).toFixed(dec)
}

function fmtPrice(v) {
  return v == null ? '—' : (Number(v) * 100).toFixed(2) + ' ct'
}

export default defineComponent({
  name: 'App',
  components: { RouterView, RouterLink },

  setup() {
    const { status, error, refresh } = useStatus()
    provide('status', status)
    provide('refresh', refresh)

    const stamp = computed(() => {
      if (!status.value) return 'Loading…'
      return new Date(status.value.timestamp).toLocaleString()
    })

    const kpiGrid   = computed(() => status.value ? fmt(status.value.grid_power_w, 0) + ' W' : '—')
    const kpiPv     = computed(() => {
      const pv = status.value?.devices?.find(d => d.soc_pct == null && (d.power_w ?? 0) < 0)
      return pv ? fmt(Math.abs(pv.power_w), 0) + ' W' : '—'
    })
    const kpiPrice  = computed(() => fmtPrice(status.value?.current_price_eur_per_kwh))
    const kpiExport = computed(() => fmtPrice(status.value?.pv_opportunity_price_eur_per_kwh))
    const kpiDryRun = computed(() => status.value ? (status.value.dry_run ? 'ON' : 'off') : '—')

    const batteryKpis = computed(() => (status.value?.ledger ?? []).map(l => ({
      label: l.device_id,
      soc: l.capacity_kwh ? fmt(l.stored_energy_kwh / l.capacity_kwh * 100, 0) + '%' : '—',
    })))

    return { status, error, refresh, stamp, kpiGrid, kpiPv, kpiPrice, kpiExport, kpiDryRun, batteryKpis }
  },

  template: `
    <div class="wrap">
      <div class="top">
        <h1>⚡ Energy Assistant</h1>
        <span class="stamp">{{ stamp }}</span>
      </div>

      <div v-if="error" style="color:var(--bad);margin-bottom:8px;font-size:.82rem">
        ⚠ {{ error }}
      </div>

      <div class="kpis">
        <div class="card"><div class="k">Grid</div><div class="v">{{ kpiGrid }}</div></div>
        <div class="card"><div class="k">Import Price</div><div class="v">{{ kpiPrice }}</div></div>
        <div class="card"><div class="k">Export Price</div><div class="v">{{ kpiExport }}</div></div>
        <div class="card">
          <div class="k">Dry Run</div>
          <div class="v" :class="status?.dry_run ? 'warn' : 'ok'">{{ kpiDryRun }}</div>
        </div>
        <div v-for="b in batteryKpis" :key="b.label" class="card">
          <div class="k">{{ b.label }}</div>
          <div class="v">{{ b.soc }}</div>
        </div>
      </div>

      <div class="tab-nav">
        <RouterLink class="tab-btn" to="/">Live</RouterLink>
        <RouterLink class="tab-btn" to="/plan">Plan</RouterLink>
      </div>

      <RouterView />
    </div>
  `,
})

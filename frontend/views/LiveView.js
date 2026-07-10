import { defineComponent, ref, inject, computed, watch, onMounted, onUnmounted } from 'vue'
import { fetchLedger, setLedgerBasis } from '../api.js'

function fmt(v, dec = 0) {
  return v == null ? '—' : Number(v).toFixed(dec)
}

function fmtPower(w) {
  if (w == null) return '—'
  const abs = Math.abs(w)
  return abs >= 1000 ? `${(w / 1000).toFixed(2)} kW` : `${Math.round(w)} W`
}

function fmtSetpointW(s) {
  if (s.setpoint_w == null) return '—'
  // EV charger sentinel values are not real watts — show charging mode label instead
  if (s.role === 'ev_charger') {
    if (s.setpoint_w > 500) return `${fmt(s.setpoint_w, 0)} W`
    if (s.setpoint_w > 0)   return 'PV mode'
    return 'stopped'
  }
  return `${fmt(s.setpoint_w, 0)} W`
}

const ROLE_ORDER = { storage: 0, ev_charger: 1, threshold_controlled: 2, consumer: 3, producer: 4, meter: 5 }
const IDLE_W = 10  // below this magnitude a flow is shown as idle

export default defineComponent({
  name: 'LiveView',

  setup() {
    const status  = inject('status')
    const refresh = inject('refresh')
    const editBasis = ref({})
    const basisBusy = ref({})

    // Fetch ledger immediately on mount — don't wait for the 30s status poll.
    const fastLedger = ref([])
    onMounted(async () => {
      try { fastLedger.value = await fetchLedger() } catch {}
    })
    // Once status arrives, keep fastLedger in sync so edits stay consistent.
    watch(status, s => { if (s?.ledger?.length) fastLedger.value = s.ledger })

    const ledger    = computed(() => fastLedger.value)
    const devices   = computed(() => status.value?.devices  ?? [])
    const setpoints = computed(() => status.value?.setpoints ?? [])

    // ── data freshness ──
    const nowTick = ref(Date.now())
    let tickTimer = null
    onMounted(() => { tickTimer = setInterval(() => { nowTick.value = Date.now() }, 5000) })
    onUnmounted(() => clearInterval(tickTimer))
    const updatedAgo = computed(() => {
      const ts = status.value?.timestamp
      if (!ts) return null
      const sec = Math.max(0, Math.round((nowTick.value - new Date(ts).getTime()) / 1000))
      return sec < 90 ? `${sec}s ago` : `${Math.round(sec / 60)} min ago`
    })

    // ── live power-flow strip ──
    // Load convention: consumption positive, production negative; grid positive = import.
    const flowTiles = computed(() => {
      const s = status.value
      if (!s) return []
      const devs = s.devices ?? []
      if (!devs.some(d => d.role)) return []  // old backend without role info
      const sum = role => devs
        .filter(d => d.role === role && d.power_w != null)
        .reduce((a, d) => a + d.power_w, 0)

      const pv   = sum('producer')
      const bat  = sum('storage')
      const ev   = sum('ev_charger')
      const grid = s.grid_power_w ?? 0
      const home = grid - pv - bat - ev

      const tiles = [
        { label: 'Solar', w: Math.abs(pv),
          dir: pv < -IDLE_W ? 'producing' : 'idle',
          cls: pv < -IDLE_W ? 'ok' : 'muted' },
        { label: 'Grid', w: Math.abs(grid),
          dir: grid > IDLE_W ? 'importing' : grid < -IDLE_W ? 'exporting' : 'idle',
          cls: grid > IDLE_W ? 'bad' : grid < -IDLE_W ? 'ok' : 'muted' },
      ]
      if (devs.some(d => d.role === 'storage')) {
        tiles.push({ label: 'Battery', w: Math.abs(bat),
          dir: bat > IDLE_W ? 'charging' : bat < -IDLE_W ? 'discharging' : 'idle',
          cls: bat > IDLE_W ? 'ok' : bat < -IDLE_W ? 'accent' : 'muted' })
      }
      if (devs.some(d => d.role === 'ev_charger')) {
        tiles.push({ label: 'EV', w: Math.abs(ev),
          dir: ev > IDLE_W ? 'charging' : 'idle',
          cls: ev > IDLE_W ? 'ok' : 'muted' })
      }
      tiles.push({ label: 'Home', w: Math.max(home, 0), dir: 'consuming', cls: 'muted' })
      return tiles
    })

    // ── device cards: live state merged with active setpoint ──
    const deviceCards = computed(() => {
      const sps = new Map(setpoints.value.map(s => [s.device_id, s]))
      return devices.value
        .map(d => ({ ...d, sp: sps.get(d.device_id) }))
        .sort((a, b) =>
          (ROLE_ORDER[a.role] ?? 9) - (ROLE_ORDER[b.role] ?? 9) ||
          (a.label ?? a.device_id).localeCompare(b.label ?? b.device_id))
    })

    function modePill(mode) {
      if (mode === 'run' || mode === 'charge_from_grid' || mode === 'charge_from_pv' || mode === 'charge_phase2')
        return 'pill pill-ok'
      if (mode === 'discharge' || mode === 'grid_feed_in') return 'pill pill-warn'
      return 'pill pill-off'
    }

    // ── ledger basis editing ──
    function startEdit(deviceId, current) {
      editBasis.value = { ...editBasis.value, [deviceId]: String(fmt(current, 4)) }
    }
    function cancelEdit(deviceId) {
      const e = { ...editBasis.value }
      delete e[deviceId]
      editBasis.value = e
    }
    async function saveBasis(deviceId) {
      const val = parseFloat(editBasis.value[deviceId])
      if (isNaN(val) || val < 0) return
      basisBusy.value = { ...basisBusy.value, [deviceId]: true }
      try {
        await setLedgerBasis(deviceId, val)
        cancelEdit(deviceId)
        fastLedger.value = await fetchLedger()
        await refresh()
      } catch (e) {
        alert('Failed: ' + e.message)
      } finally {
        basisBusy.value = { ...basisBusy.value, [deviceId]: false }
      }
    }

    return { status, ledger, devices, setpoints, editBasis, basisBusy,
             flowTiles, deviceCards, updatedAgo, modePill,
             fmt, fmtPower, fmtSetpointW, startEdit, cancelEdit, saveBasis }
  },

  template: `
    <div>
      <div class="live-fresh" v-if="updatedAgo">
        <span class="pulse-dot"></span> updated {{ updatedAgo }}
      </div>

      <div v-if="flowTiles.length" class="flow-strip">
        <div v-for="t in flowTiles" :key="t.label" class="flow-tile">
          <div class="k">{{ t.label }}</div>
          <div class="v">{{ fmtPower(t.w) }}</div>
          <div class="flow-dir" :class="t.cls">{{ t.dir }}</div>
        </div>
      </div>

      <div class="full panel">
        <h2>Devices</h2>
        <div class="dev-cards" v-if="deviceCards.length">
          <div v-for="d in deviceCards" :key="d.device_id" class="dev-card">
            <div class="dev-card-header">
              <span class="status-dot" :class="d.available ? 'on' : 'off'"
                :title="d.available ? 'online' : 'unavailable'"></span>
              <span class="dev-label">{{ d.label ?? d.device_id }}</span>
              <span class="dev-power" :class="{ producing: (d.power_w ?? 0) < 0 }">
                {{ fmtPower(d.power_w) }}
              </span>
            </div>
            <div v-if="d.soc_pct != null" class="soc-row">
              <span class="soc-num">{{ fmt(d.soc_pct, 0) }}%</span>
              <div class="soc-bar">
                <div class="soc-fill" :style="{ width: Math.min(Math.max(d.soc_pct, 0), 100) + '%' }"></div>
              </div>
            </div>
            <div v-if="d.sp" class="dev-sp">
              <span :class="modePill(d.sp.mode)">{{ (d.sp.mode ?? '—').replace(/_/g, ' ') }}</span>
              <span class="dev-sp-w">{{ fmtSetpointW(d.sp) }}</span>
              <span v-if="d.sp.grid_allowed != null" class="dev-flag" :class="{ off: !d.sp.grid_allowed }">
                grid {{ d.sp.grid_allowed ? '✓' : '✗' }}
              </span>
              <span v-if="d.sp.export_allowed != null" class="dev-flag" :class="{ off: !d.sp.export_allowed }">
                export {{ d.sp.export_allowed ? '✓' : '✗' }}
              </span>
            </div>
          </div>
        </div>
        <div v-else style="color:var(--muted);font-size:.85rem">Loading…</div>
      </div>

      <div class="full panel">
        <h2>Battery Ledger</h2>
        <div class="table-scroll">
          <table>
            <thead>
              <tr><th>Device</th><th>Stored</th><th class="num">SoC</th><th class="num">Basis €/kWh</th></tr>
            </thead>
            <tbody>
              <tr v-for="l in ledger" :key="l.device_id">
                <td>{{ l.device_id }}</td>
                <td class="ledger-stored">
                  <span class="num">{{ fmt(l.stored_energy_kwh, 1) }} / {{ fmt(l.capacity_kwh, 1) }} kWh</span>
                  <div class="soc-bar" v-if="l.capacity_kwh">
                    <div class="soc-fill"
                      :style="{ width: Math.min(Math.max(l.stored_energy_kwh / l.capacity_kwh * 100, 0), 100) + '%' }"></div>
                  </div>
                </td>
                <td class="num">{{ l.capacity_kwh ? fmt(l.stored_energy_kwh / l.capacity_kwh * 100, 0) + '%' : '—' }}</td>
                <td class="num">
                  <template v-if="editBasis[l.device_id] !== undefined">
                    <div class="basis-edit">
                      <input type="number" inputmode="decimal" step="0.001" min="0"
                        v-model="editBasis[l.device_id]" />
                      <button class="btn btn-primary"
                        :disabled="basisBusy[l.device_id]"
                        @click="saveBasis(l.device_id)">Save</button>
                      <button class="btn btn-neutral" @click="cancelEdit(l.device_id)">×</button>
                    </div>
                  </template>
                  <template v-else>
                    <span class="basis-value">{{ fmt(l.cost_basis_eur_per_kwh, 4) }}</span>
                    <button class="btn btn-neutral btn-edit" title="Edit cost basis"
                      @click="startEdit(l.device_id, l.cost_basis_eur_per_kwh)">✎</button>
                  </template>
                </td>
              </tr>
              <tr v-if="!ledger.length">
                <td colspan="4" style="color:var(--muted)">Loading…</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `,
})

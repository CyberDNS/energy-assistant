import { defineComponent, ref, inject, computed, watch, onMounted } from 'vue'
import { fetchLedger, setLedgerBasis } from '../api.js'

function fmt(v, dec = 0) {
  return v == null ? '—' : Number(v).toFixed(dec)
}

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

    const ledger   = computed(() => fastLedger.value)
    const devices  = computed(() => status.value?.devices  ?? [])
    const setpoints = computed(() => status.value?.setpoints ?? [])

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
             fmt, startEdit, cancelEdit, saveBasis }
  },

  template: `
    <div>
      <div class="row2">
        <div class="panel">
          <h2>Devices</h2>
          <table>
            <thead><tr><th>Device</th><th>Power W</th><th>SoC %</th><th>OK</th></tr></thead>
            <tbody>
              <tr v-for="d in devices" :key="d.device_id">
                <td>{{ d.device_id }}</td>
                <td>{{ d.power_w != null ? fmt(d.power_w, 0) : '—' }}</td>
                <td>{{ d.soc_pct != null ? fmt(d.soc_pct, 1) : '—' }}</td>
                <td>{{ d.available ? '✓' : '✗' }}</td>
              </tr>
              <tr v-if="!devices.length">
                <td colspan="4" style="color:var(--muted)">Loading…</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="panel">
          <h2>Active Setpoints</h2>
          <table>
            <thead><tr><th>Device</th><th>Mode</th><th>Policy</th><th>W</th></tr></thead>
            <tbody>
              <tr v-for="s in setpoints" :key="s.device_id">
                <td>{{ s.device_id }}</td>
                <td>{{ s.mode ?? '—' }}</td>
                <td>{{ s.charge_policy ?? s.discharge_policy ?? '—' }}</td>
                <td>{{ s.setpoint_w != null ? fmt(s.setpoint_w, 0) : '—' }}</td>
              </tr>
              <tr v-if="!setpoints.length">
                <td colspan="4" style="color:var(--muted)">{{ status ? 'No active setpoints' : 'Loading…' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="full panel">
        <h2>Battery Ledger</h2>
        <table>
          <thead>
            <tr><th>Device</th><th>Stored kWh</th><th>SoC %</th><th>Capacity kWh</th><th>Basis €/kWh</th></tr>
          </thead>
          <tbody>
            <tr v-for="l in ledger" :key="l.device_id">
              <td>{{ l.device_id }}</td>
              <td>{{ fmt(l.stored_energy_kwh, 2) }}</td>
              <td>{{ l.capacity_kwh ? fmt(l.stored_energy_kwh / l.capacity_kwh * 100, 0) + '%' : '—' }}</td>
              <td>{{ fmt(l.capacity_kwh, 1) }}</td>
              <td>
                <template v-if="editBasis[l.device_id] !== undefined">
                  <div class="basis-edit">
                    <input type="number" step="0.001" min="0" v-model="editBasis[l.device_id]" />
                    <button class="btn btn-primary" style="padding:3px 8px;font-size:.78rem"
                      :disabled="basisBusy[l.device_id]"
                      @click="saveBasis(l.device_id)">Save</button>
                    <button class="btn btn-neutral" style="padding:3px 8px;font-size:.78rem"
                      @click="cancelEdit(l.device_id)">×</button>
                  </div>
                </template>
                <template v-else>
                  <span style="cursor:pointer" @click="startEdit(l.device_id, l.cost_basis_eur_per_kwh)"
                    title="Click to edit">
                    {{ fmt(l.cost_basis_eur_per_kwh, 4) }} ✎
                  </span>
                </template>
              </td>
            </tr>
            <tr v-if="!ledger.length">
              <td colspan="5" style="color:var(--muted)">Loading…</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  `,
})

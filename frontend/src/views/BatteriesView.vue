<script setup lang="ts">
import type { Ref } from 'vue'
import type { LedgerEntry, Status } from '../composables/usePowerFlow'
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchControllable, fetchLedger, setLedgerBasis } from '../api.js'
import ControllableDeviceCard from '../components/ControllableDeviceCard.vue'
import DeviceCard from '../components/DeviceCard.vue'
import DeviceSavings from '../components/DeviceSavings.vue'
import { deviceLink } from '../lib/deviceLinks'
import { useLiveDevices } from '../composables/useLiveDevices'
import { labelOf, planVersion } from '../composables/useStatus.js'
import { fmt } from '../lib/format'

// Batteries: live state, what the plan does with them next, and the ledger
// (how much energy is stored and what it cost to put there).
const status = inject('status') as Ref<Status | null>
const refreshStatus = inject('refresh') as () => Promise<void>
const batteries = useLiveDevices(status, ['battery'])

// ── planned charging and discharging ──
/* eslint-disable @typescript-eslint/no-explicit-any */
const planned = ref<any[]>([])
let timer: ReturnType<typeof setInterval> | undefined
async function loadPlan() {
  try {
    planned.value = ((await fetchControllable())?.devices ?? []).filter((d: any) => d.category === 'battery')
  } catch { /* keep the last plan */ }
}
onMounted(() => { loadPlan(); timer = setInterval(loadPlan, 60_000) })
onUnmounted(() => clearInterval(timer))
watch(planVersion, loadPlan)

// ── ledger ──
// Fetched right away; afterwards every status push keeps it current.
const ledger = ref<LedgerEntry[]>([])
onMounted(async () => {
  try { ledger.value = await fetchLedger() } catch { /* the status brings it */ }
})
watch(status, (s) => { if (s?.ledger?.length) ledger.value = s.ledger })

const editing = ref<Record<string, string>>({})
const busy = ref<Record<string, boolean>>({})
const error = ref<string | null>(null)

function startEdit(id: string, current: number) {
  error.value = null
  editing.value = { ...editing.value, [id]: fmt(current, 4) }
}
function cancelEdit(id: string) {
  const rest = { ...editing.value }
  delete rest[id]
  editing.value = rest
}
async function save(id: string) {
  const value = Number.parseFloat(editing.value[id] ?? '')
  if (Number.isNaN(value) || value < 0) return
  busy.value = { ...busy.value, [id]: true }
  try {
    await setLedgerBasis(id, value)
    cancelEdit(id)
    ledger.value = await fetchLedger()
    await refreshStatus()
  } catch (e) {
    error.value = `Could not save the cost basis: ${(e as Error).message}`
  } finally {
    busy.value = { ...busy.value, [id]: false }
  }
}
const pct = (l: LedgerEntry) => l.capacity_kwh ? Math.min(Math.max(l.stored_energy_kwh / l.capacity_kwh * 100, 0), 100) : 0
const loading = computed(() => status.value == null)
</script>

<template>
  <div>
    <div v-if="batteries.length" class="dev-cards full">
      <DeviceCard v-for="d in batteries" :key="d.device_id" :device="d" :to="deviceLink(d)" class="bg-card" />
    </div>
    <div v-else-if="loading" class="text-muted-foreground py-4 text-sm">Loading…</div>

    <div v-if="planned.length" class="full panel">
      <h2>Plan</h2>
      <div class="ctrl-list">
        <ControllableDeviceCard v-for="d in planned" :key="d.device_id" :device="d" />
      </div>
    </div>

    <DeviceSavings group="batteries" />

    <div class="full panel">
      <h2>Stored energy and its cost</h2>
      <p class="panel-hint">
        The ledger tracks what the energy in each battery cost to store (grid price when charged from the grid,
        the export price given up when charged from PV). The planner only discharges when that pays off.
      </p>
      <div v-if="error" class="ev-error" role="alert">{{ error }}</div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr><th>Battery</th><th>Stored</th><th class="num">SoC</th><th class="num">Cost basis €/kWh</th></tr>
          </thead>
          <tbody>
            <tr v-for="l in ledger" :key="l.device_id">
              <td>{{ labelOf(l.device_id) }}</td>
              <td class="ledger-stored">
                <span class="num">{{ fmt(l.stored_energy_kwh, 1) }} / {{ fmt(l.capacity_kwh, 1) }} kWh</span>
                <div v-if="l.capacity_kwh" class="soc-bar">
                  <div class="soc-fill" :style="{ width: pct(l) + '%' }" />
                </div>
              </td>
              <td class="num">{{ l.capacity_kwh ? fmt(pct(l), 0) + '%' : '—' }}</td>
              <td class="num">
                <div v-if="editing[l.device_id] !== undefined" class="basis-edit">
                  <input
                    v-model="editing[l.device_id]" type="number" inputmode="decimal" step="0.001" min="0"
                    :aria-label="'Cost basis of ' + labelOf(l.device_id)"
                  >
                  <button class="btn btn-primary" :disabled="busy[l.device_id]" @click="save(l.device_id)">Save</button>
                  <button class="btn btn-neutral" aria-label="Cancel" @click="cancelEdit(l.device_id)">×</button>
                </div>
                <template v-else>
                  <span class="basis-value">{{ fmt(l.cost_basis_eur_per_kwh, 4) }}</span>
                  <button
                    class="btn btn-neutral btn-edit" title="Edit cost basis"
                    :aria-label="'Edit cost basis of ' + labelOf(l.device_id)"
                    @click="startEdit(l.device_id, l.cost_basis_eur_per_kwh)"
                  >
                    ✎
                  </button>
                </template>
              </td>
            </tr>
            <tr v-if="!ledger.length">
              <td colspan="4" class="text-muted-foreground">{{ loading ? 'Loading…' : 'No battery in the ledger.' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '../composables/usePowerFlow'
import { computed, inject, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { fetchControllable, fetchLearned } from '../api.js'
import ControllableDeviceCard from '../components/ControllableDeviceCard.vue'
import DeviceCard from '../components/DeviceCard.vue'
import DeviceControls from '../components/DeviceControls.vue'
import DeviceHistory from '../components/DeviceHistory.vue'
import DeviceSavings from '../components/DeviceSavings.vue'
import LearnedModel from '../components/LearnedModel.vue'
import { useLiveDevices } from '../composables/useLiveDevices'
import { usePageTitle } from '../composables/usePageTitle'
import { planVersion } from '../composables/useStatus.js'
import { CATEGORY_TITLES } from '../lib/categories'
import { deviceLink, deviceNav, learnedDeviceId } from '../lib/deviceLinks'
import { fmt } from '../lib/format'
import { navItem } from '../nav'

// One device, with everything about it in the same order for every kind of
// device: now, plan, history, what it saved, what was learned about it.
// Sections a device has nothing for are left out.
/* eslint-disable @typescript-eslint/no-explicit-any */
const route = useRoute()
const status = inject('status') as Ref<Status | null>
const refresh = inject('refresh') as () => Promise<void>
const devices = useLiveDevices(status)

const id = computed(() => String(route.params.id ?? ''))
const device = computed(() => devices.value.find(d => d.device_id === id.value) ?? null)
const category = computed(() => device.value?.category ?? null)

const nav = computed(() => deviceNav(category.value))
usePageTitle(
  () => device.value?.label ?? (status.value ? 'Unknown device' : ''),
  () => {
    const item = navItem(nav.value)
    return item ? [{ title: item.title, to: item.to }] : []
  },
  nav,
)

// ── plan and learned models of this device ──
const planned = ref<any | null>(null)
const learned = ref<any[]>([])
let timer: ReturnType<typeof setInterval> | undefined

async function loadPlan() {
  try {
    planned.value = ((await fetchControllable())?.devices ?? []).find((d: any) => d.device_id === id.value) ?? null
  } catch { /* keep the last plan */ }
}
async function loadLearned() {
  try {
    learned.value = (await fetchLearned({ deviceId: id.value })).filter((i: any) => learnedDeviceId(i) === id.value)
  } catch { /* the section stays away */ }
  // A link from the Learning overview points at this section.
  if (route.hash === '#learned' && learned.value.length) {
    await nextTick()
    document.getElementById('learned')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}
function load() { loadPlan(); loadLearned() }
onMounted(() => { load(); timer = setInterval(loadPlan, 60_000) })
onUnmounted(() => clearInterval(timer))
watch(id, load)
watch(planVersion, loadPlan)

// ── battery: what is stored and what it cost ──
const ledger = computed(() => (status.value?.ledger ?? []).find(l => l.device_id === id.value) ?? null)

// The savings accounting treats all batteries as one storage.
const savingsGroup = computed(() => (category.value === 'battery' ? 'batteries' : id.value))
</script>

<template>
  <div v-if="device" :key="id">
    <div class="full device-now">
      <DeviceCard :device="device" class="bg-card">
        <dl class="facts">
          <div><dt>Kind</dt><dd>{{ CATEGORY_TITLES[category ?? ''] ?? 'Device' }}</dd></div>
          <template v-if="ledger">
            <div><dt>Stored</dt><dd>{{ fmt(ledger.stored_energy_kwh, 1) }} of {{ fmt(ledger.capacity_kwh, 1) }} kWh</dd></div>
            <div>
              <dt>Cost of the stored energy</dt>
              <dd>{{ fmt(ledger.cost_basis_eur_per_kwh * 100, 1) }} ct/kWh <RouterLink class="section-link" to="/batteries">Ledger ›</RouterLink></dd>
            </div>
          </template>
          <div v-if="device.available === false"><dt>State</dt><dd class="bad">Not responding</dd></div>
        </dl>
      </DeviceCard>
    </div>

    <DeviceControls :device-id="id" @changed="refresh(); loadPlan()" />

    <div v-if="planned" class="full panel" data-testid="device-plan">
      <h2>Plan</h2>
      <div class="ctrl-list">
        <ControllableDeviceCard :device="planned" />
      </div>
    </div>

    <DeviceHistory :device-id="id" :category="category" />

    <DeviceSavings :group="savingsGroup" />

    <div v-for="item in learned" id="learned" :key="item.id" class="full panel">
      <h2>What was learned</h2>
      <LearnedModel :item="item" @refresh="loadLearned" />
    </div>

    <p class="footnote">
      <RouterLink class="section-link" to="/settings/devices">Change this device in Settings ›</RouterLink>
    </p>
  </div>

  <div v-else-if="status" class="panel empty-state">
    <h2>Unknown device “{{ id }}”</h2>
    <p>It may have been renamed or removed.</p>
    <RouterLink class="btn btn-neutral" to="/devices">All devices</RouterLink>
  </div>
  <div v-else class="text-muted-foreground py-4 text-sm">Loading…</div>
</template>

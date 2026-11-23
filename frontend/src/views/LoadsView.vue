<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '../composables/usePowerFlow'
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { fetchControllable } from '../api.js'
import ControllableDeviceCard from '../components/ControllableDeviceCard.vue'
import DeviceCard from '../components/DeviceCard.vue'
import { deviceLink } from '../lib/deviceLinks'
import { useLiveDevices } from '../composables/useLiveDevices'
import { planVersion } from '../composables/useStatus.js'

// Heating and other loads the planner may shift: live state and what is
// planned for each.
const status = inject('status') as Ref<Status | null>
const live = useLiveDevices(status, ['thermal_load', 'switch'])

/* eslint-disable @typescript-eslint/no-explicit-any */
const planned = ref<any[]>([])
const loaded = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
async function load() {
  try {
    planned.value = ((await fetchControllable())?.devices ?? [])
      .filter((d: any) => d.category !== 'vehicle' && d.category !== 'battery')
  } catch { /* keep the last plan */ } finally {
    loaded.value = true
  }
}
onMounted(() => { load(); timer = setInterval(load, 60_000) })
onUnmounted(() => clearInterval(timer))
watch(planVersion, load)

const empty = computed(() => loaded.value && status.value != null && !live.value.length && !planned.value.length)
</script>

<template>
  <div>
    <div v-if="live.length" class="dev-cards full">
      <DeviceCard v-for="d in live" :key="d.device_id" :device="d" :to="deviceLink(d)" class="bg-card" />
    </div>

    <div v-if="planned.length" class="full panel">
      <h2>Plan</h2>
      <p class="panel-hint">
        When each load is planned to run. Loads with a temperature band run when electricity is cheap or PV is
        available, and always before the temperature leaves the band.
      </p>
      <div class="ctrl-list">
        <ControllableDeviceCard v-for="d in planned" :key="d.device_id" :device="d" />
      </div>
    </div>

    <div v-if="empty" class="panel empty-state">
      <h2>No controllable loads yet</h2>
      <p>Add a heater, a boiler or another switchable load so the planner can run it at the best time.</p>
      <RouterLink class="btn btn-primary" to="/settings/devices">Open device settings</RouterLink>
    </div>
  </div>
</template>

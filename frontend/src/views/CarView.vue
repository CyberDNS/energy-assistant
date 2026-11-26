<script setup lang="ts">
import { computed, inject } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import ControllableDeviceCard from '../components/ControllableDeviceCard.vue'
import DeviceControls from '../components/DeviceControls.vue'
import DeviceHistory from '../components/DeviceHistory.vue'
import DeviceSavings from '../components/DeviceSavings.vue'
import EvCard from '../components/EvCard.vue'
import { useCars } from '../composables/useCars'
import { usePageTitle } from '../composables/usePageTitle'
import { planning } from '../composables/useStatus.js'

// One car: #/cars/<vehicle>, matched by vehicle id or label, case-insensitively.
// Also what an iframe in a Home Assistant dashboard shows (…?embed=1).
const route = useRoute()
const embed = inject('embed', computed(() => false))
const refreshStatus = inject('refresh', async () => {}) as () => Promise<void>
const { cars, chargers, loaded, refresh, planOf } = useCars()

const wanted = computed(() => String(route.params.vehicle ?? '').trim().toLowerCase())
const car = computed(() => cars.value.find(e =>
  e.vehicle_id.toLowerCase() === wanted.value || e.label?.toLowerCase() === wanted.value))
const plan = computed(() => car.value && planOf(car.value.vehicle_id))

usePageTitle(() => car.value?.label ?? (loaded.value ? 'Unknown car' : ''), [{ title: 'Cars', to: '/cars' }])
</script>

<template>
  <div>
    <!-- Without the header (embed mode) the page says itself that a new plan is on its way. -->
    <transition name="fade">
      <p v-if="embed && planning" class="plan-busy" style="margin-bottom:8px">
        <span class="spinner" /> Recalculating plan…
      </p>
    </transition>

    <template v-if="car">
      <div class="full">
        <EvCard :key="car.vehicle_id" :ev="car" :chargers="chargers" @refresh="refresh" />
      </div>
      <div v-if="plan" class="full panel">
        <h2>Charging plan</h2>
        <div class="ctrl-list">
          <ControllableDeviceCard :device="plan" />
        </div>
      </div>
      <!-- An embedded car card stays what it was: the car and its charging plan. -->
      <template v-if="!embed">
        <DeviceControls :device-id="car.vehicle_id" @changed="refreshStatus(); refresh()" />
        <DeviceHistory :device-id="car.vehicle_id" :power-device-id="car.charger_id || null" category="vehicle" />
        <DeviceSavings :group="car.vehicle_id" />
      </template>
    </template>
    <div v-else-if="loaded" class="panel empty-state">
      <h2>Unknown car “{{ route.params.vehicle }}”</h2>
      <p v-if="cars.length">Available: {{ cars.map(e => e.vehicle_id).join(', ') }}</p>
      <RouterLink v-if="!embed" class="btn btn-neutral" to="/cars">All cars</RouterLink>
    </div>
  </div>
</template>

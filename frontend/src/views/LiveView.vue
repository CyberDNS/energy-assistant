<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '../composables/usePowerFlow'
import { computed, inject } from 'vue'
import { RouterLink } from 'vue-router'
import DeviceCard from '../components/DeviceCard.vue'
import EvCard from '../components/EvCard.vue'
import GuestCard from '../components/GuestCard.vue'
import { useCars } from '../composables/useCars'
import { useLiveDevices } from '../composables/useLiveDevices'
import { usePowerFlow } from '../composables/usePowerFlow'
import { fmtPower } from '../lib/format'

// Home: what the house is doing right now, the cars, and every device.
const status = inject('status') as Ref<Status | null>
const { tiles } = usePowerFlow(status)
const devices = useLiveDevices(status)
const { cars, chargers, refresh } = useCars()
const loading = computed(() => status.value == null)
</script>

<template>
  <div>
    <div v-if="tiles.length" class="flow-strip" data-testid="flow-strip">
      <div v-for="t in tiles" :key="t.id" class="flow-tile">
        <div class="k">{{ t.label }}</div>
        <div class="v">{{ fmtPower(t.w) }}</div>
        <div class="flow-dir" :class="t.tone">{{ t.dir }}</div>
      </div>
    </div>

    <section v-if="cars.length" class="full">
      <div class="section-head">
        <h2>Cars</h2>
        <RouterLink to="/cars" class="section-link">All cars ›</RouterLink>
      </div>
      <div class="ev-cards">
        <EvCard
          v-for="ev in cars" :key="ev.vehicle_id" :ev="ev" :chargers="chargers" collapsible
          :to="'/cars/' + encodeURIComponent(ev.vehicle_id)" @refresh="refresh"
        />
      </div>
      <GuestCard v-if="chargers.length" :chargers="chargers" class="mt-3" @refresh="refresh" />
    </section>

    <section class="full">
      <div class="section-head">
        <h2>Devices</h2>
      </div>
      <div v-if="devices.length" class="dev-cards">
        <DeviceCard v-for="d in devices" :key="d.device_id" :device="d" class="bg-card" />
      </div>
      <div v-else class="text-muted-foreground text-sm">{{ loading ? 'Loading…' : 'No devices yet.' }}</div>
    </section>
  </div>
</template>

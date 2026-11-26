<script setup lang="ts">
import { ChevronRight } from '@lucide/vue'
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { fetchLearned } from '../api.js'
import { labelOf } from '../composables/useStatus.js'
import { deviceLink, learnedDeviceId } from '../lib/deviceLinks'
import { LEARNED_KINDS, learnedHeadline, learnedKind, learnedStatus } from '../lib/learned'
import { vehicles } from '../stores/inventory'

// Everything Energy Assistant learns, at a glance: which models exist,
// whether planning uses them and how accurate they are.  The charts of a
// model are on its device's page.
/* eslint-disable @typescript-eslint/no-explicit-any */
const items = ref<any[]>([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    items.value = await fetchLearned({ summary: true })
  } catch (e) {
    error.value = String((e as Error).message ?? e)
  } finally {
    loading.value = false
  }
})

const groups = computed(() => LEARNED_KINDS
  .map(kind => ({ ...kind, items: items.value.filter(i => learnedKind(i) === kind.id) }))
  .filter(g => g.items.length))

const counts = computed(() => {
  const ok = items.value.filter(i => learnedStatus(i).cls === 'pill-ok').length
  return { ok, total: items.value.length }
})

function link(item: any): string {
  const id = learnedDeviceId(item)
  const isCar = vehicles.value.some(v => v.vehicle_id === id)
  return deviceLink({ device_id: id, category: isCar ? 'vehicle' : null }) + '#learned'
}
const name = (item: any) => item.label ?? labelOf(learnedDeviceId(item))
</script>

<template>
  <div>
    <div v-if="loading" class="text-muted-foreground py-4 text-sm">Loading…</div>
    <div v-else-if="error" class="panel ev-error">Could not load the learned models: {{ error }}</div>
    <div v-else-if="!items.length" class="panel empty-state">
      <h2>Nothing is learned yet</h2>
      <p>
        Give a consumer a learned forecast, or add a battery or a load kept between two thresholds
        (aquarium cooler, AC, …), and what Energy Assistant learns about it appears here.
      </p>
      <RouterLink class="btn btn-primary" to="/settings/devices">Open device settings</RouterLink>
    </div>

    <template v-else>
      <p class="panel-hint" style="margin:0 0 14px">
        {{ counts.ok }} of {{ counts.total }} learned models are in use by the planner. Open a device to see
        its charts.
      </p>
      <div v-for="g in groups" :key="g.id" class="panel full" :data-testid="'learned-' + g.id">
        <h2>{{ g.title }}</h2>
        <p class="panel-hint">{{ g.about }}</p>
        <div class="link-list">
          <RouterLink v-for="i in g.items" :key="i.id" :to="link(i)" class="link-row">
            <span class="link-row-main">{{ name(i) }}</span>
            <span class="link-row-sub hidden sm:inline">{{ learnedHeadline(i) }}</span>
            <span :class="['pill', learnedStatus(i).cls]">{{ learnedStatus(i).text }}</span>
            <ChevronRight class="link-row-chevron" />
          </RouterLink>
        </div>
      </div>
    </template>
  </div>
</template>

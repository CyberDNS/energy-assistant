<script setup>
import { computed, onMounted } from 'vue'
import PlotlyChart from './PlotlyChart.js'
import RangeButtons from './RangeButtons.vue'
import { useSavings } from '../composables/useSavings.js'

// What one savings group saved: the batteries together, a car, or a
// controlled device.  Renders nothing for a device that is not controlled,
// and nothing before the first savings are booked.
const props = defineProps({
  // The group id of /api/savings: "batteries", a vehicle id or a device id.
  group: { type: String, required: true },
})

const { RANGES, range, pending, loaded, load, eur, panels, buckets } = useSavings()
const panel = computed(() => panels.value.find(p => p.id === props.group) ?? null)
onMounted(() => load(range.value))
</script>

<template>
  <div v-if="loaded && panel" class="full panel" data-testid="device-savings">
    <div class="panel-head">
      <h2>What it saved</h2>
      <span class="savings-group-saving" :class="panel.saving >= 0 ? 'ok' : 'bad'">{{ eur(panel.saving, true) }}</span>
      <span class="ev-actions-spacer" />
      <RangeButtons :ranges="RANGES" :active="pending ?? range" @pick="load" />
    </div>
    <p v-if="!buckets.length" class="panel-hint">Nothing booked in this range yet.</p>
    <p v-else-if="panel.note" class="panel-hint">{{ panel.note }}</p>
    <PlotlyChart v-if="panel.traces.length && buckets.length" :traces="panel.traces" :layout="panel.layout" :height="panel.height" />
    <p v-if="group === 'batteries'" class="footnote">All batteries together: the saving is accounted for the storage as a whole.</p>
  </div>
</template>

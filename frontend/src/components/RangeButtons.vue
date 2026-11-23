<script setup lang="ts">
// The segmented time-range switch above a chart.
defineProps<{
  ranges: readonly { label: string }[]
  active: { label: string } | null | undefined
}>()
defineEmits<{ pick: [range: never] }>()
</script>

<template>
  <div class="range-btns" role="group" aria-label="Time range">
    <button
      v-for="r in ranges" :key="r.label" type="button"
      :class="['range-btn', active?.label === r.label ? 'active' : '']" :aria-pressed="active?.label === r.label"
      @click="$emit('pick', r as never)"
    >
      {{ r.label }}
    </button>
  </div>
</template>

<script>
import { defineComponent, computed } from 'vue'
import { DAYS, formatDays, parseDays } from './weekdays.js'

// Weekdays of a time-of-use window as seven toggles (Mo … Su).  Writes the
// compact list form (`[mon-fri, sun]`); all seven = unset (every day), shown
// in a lighter tone so it reads as the default.  A value it cannot read stays
// as it is until a day is toggled.
export default defineComponent({
  name: 'WeekdaysEditor',
  props: {
    modelValue: { default: undefined },
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const selected = computed(() => parseDays(props.modelValue))
    const labels = DAYS.map(d => d[0].toUpperCase() + d[1])
    function toggle(i) {
      const next = new Set(selected.value ?? [])
      if (next.has(i)) next.delete(i)
      else next.add(i)
      emit('update:modelValue', formatDays(next))
    }
    const everyDay = computed(() => props.modelValue == null)
    return { DAYS, labels, selected, toggle, everyDay }
  },
})
</script>

<template>
  <div :class="['cfg-weekdays', { 'is-every-day': everyDay }]" role="group" :aria-label="ariaLabel"
       :title="everyDay ? 'Every day (default)' : null">
    <button v-for="(d, i) in DAYS" :key="d" type="button" class="cfg-weekday"
            :class="{ 'is-on': selected?.has(i) }" :aria-pressed="!!selected?.has(i)" :title="d"
            @click="toggle(i)">{{ labels[i] }}</button>
    <span v-if="selected === null" class="cfg-weekdays-bad">{{ JSON.stringify(modelValue) }} (invalid)</span>
  </div>
</template>

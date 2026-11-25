<script>
import { defineComponent, computed, ref, watch } from 'vue'

// A day of the year without the year (MM-DD, e.g. a season's first day) as
// a day and a month dropdown.  The value is written once both are chosen.  A
// value it cannot read stays visible, marked invalid.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const LENGTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

export function parseMonthDay(value) {
  const m = /^(\d{1,2})-(\d{1,2})$/.exec(String(value ?? '').trim())
  if (!m) return null
  const month = Number(m[1]), day = Number(m[2])
  return month >= 1 && month <= 12 && day >= 1 && day <= LENGTH[month - 1] ? { month, day } : null
}

export default defineComponent({
  name: 'MonthDayInput',
  props: {
    modelValue: { default: undefined },
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const day = ref(''), month = ref('')
    const bad = computed(() => props.modelValue != null && props.modelValue !== '' && !parseMonthDay(props.modelValue))
    watch(() => props.modelValue, (v) => {
      const p = parseMonthDay(v)
      if (p) { day.value = String(p.day); month.value = String(p.month) }
      else if (v == null || v === '') { day.value = ''; month.value = '' }
    }, { immediate: true })
    const days = computed(() => Array.from({ length: month.value ? LENGTH[month.value - 1] : 31 }, (_, i) => String(i + 1)))
    function pick(which, v) {
      if (which === 'day') day.value = v
      else month.value = v
      if (!day.value && !month.value) return emit('update:modelValue', undefined)
      if (!day.value || !month.value) return
      const d = Math.min(Number(day.value), LENGTH[Number(month.value) - 1])
      day.value = String(d)
      emit('update:modelValue', `${month.value.padStart(2, '0')}-${String(d).padStart(2, '0')}`)
    }
    return { MONTHS, days, day, month, bad, pick }
  },
})
</script>

<template>
  <div class="cfg-time" role="group" :aria-label="ariaLabel">
    <select :value="day" :aria-label="(ariaLabel ? ariaLabel + ', ' : '') + 'day'" @change="pick('day', $event.target.value)">
      <option value="">day</option>
      <option v-for="d in days" :key="d" :value="d">{{ d }}</option>
    </select>
    <select :value="month" :aria-label="(ariaLabel ? ariaLabel + ', ' : '') + 'month'" @change="pick('month', $event.target.value)">
      <option value="">month</option>
      <option v-for="(m, i) in MONTHS" :key="m" :value="String(i + 1)">{{ m }}</option>
    </select>
    <span v-if="bad" class="cfg-weekdays-bad">{{ modelValue }} (invalid)</span>
  </div>
</template>

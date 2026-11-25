<script>
import { defineComponent, computed } from 'vue'

// A time on a quarter hour (HH:MM) as an hour and a minute dropdown, so only
// valid times can be picked.  `end` also offers 24:00 (the end of the day).
// A value that is not on a quarter hour stays visible, marked invalid.
export default defineComponent({
  name: 'TimeInput',
  props: {
    modelValue: { default: undefined },
    end:        { type: Boolean, default: false },
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const pad = (n) => String(n).padStart(2, '0')
    const hours = computed(() => Array.from({ length: props.end ? 25 : 24 }, (_, i) => pad(i)))
    const MINUTES = ['00', '15', '30', '45']
    const parts = computed(() => {
      const m = /^(\d{1,2}):(\d{2})$/.exec(String(props.modelValue ?? '').trim())
      if (!m) return { hour: '', minute: '', bad: props.modelValue != null && props.modelValue !== '' }
      const hour = pad(Number(m[1]))
      const bad = !hours.value.includes(hour) || !MINUTES.includes(m[2]) || (hour === '24' && m[2] !== '00')
      return { hour, minute: m[2], bad }
    })
    function set(hour, minute) {
      if (!hour) return emit('update:modelValue', undefined)
      emit('update:modelValue', `${hour}:${hour === '24' || !MINUTES.includes(minute) ? '00' : minute}`)
    }
    return { hours, MINUTES, parts, set }
  },
})
</script>

<template>
  <div class="cfg-time" role="group" :aria-label="ariaLabel">
    <select :value="parts.bad ? '' : parts.hour" :aria-label="(ariaLabel ? ariaLabel + ', ' : '') + 'hour'"
            @change="set($event.target.value, parts.minute)">
      <option value="">--</option>
      <option v-for="h in hours" :key="h" :value="h">{{ h }}</option>
    </select>
    <span class="cfg-time-sep">:</span>
    <select :value="parts.bad ? '' : parts.minute" :disabled="!parts.hour || parts.hour === '24'"
            :aria-label="(ariaLabel ? ariaLabel + ', ' : '') + 'minute'" @change="set(parts.hour, $event.target.value)">
      <option value="" disabled>--</option>
      <option v-for="m in MINUTES" :key="m" :value="m">{{ m }}</option>
    </select>
    <span v-if="parts.bad" class="cfg-weekdays-bad">{{ modelValue }} (invalid)</span>
  </div>
</template>

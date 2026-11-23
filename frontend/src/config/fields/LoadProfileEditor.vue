<script>
import { defineComponent, computed } from 'vue'

const DAY_TYPES = ['weekdays', 'weekends', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
const ROW_FIELDS = {
  hour: { kind: 'int', required: true, minimum: 0, maximum: 23, label: 'from hour', description: 'Local hour this segment starts' },
  consumed_kwh: { kind: 'float', required: true, minimum: 0, label: 'kWh until the next row', description: 'Energy consumed from this hour until the next row (or midnight)' },
}

// config.yaml accepts { weekdays: [...] } and the list form [{ weekdays: [...] }].
function normalize(raw) {
  if (Array.isArray(raw)) {
    const out = {}
    for (const item of raw) {
      if (item && typeof item === 'object') for (const [k, v] of Object.entries(item)) if (Array.isArray(v)) out[k] = v
    }
    return out
  }
  return raw && typeof raw === 'object' ? raw : {}
}

// A static load profile: per day type, the energy consumed in each part of
// the day.  One table per day type, with the day's total.
export default defineComponent({
  name: 'LoadProfileEditor',
  props: { modelValue: { default: undefined } },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const profile = computed(() => normalize(props.modelValue))
    const used = computed(() => Object.keys(profile.value))
    const free = computed(() => DAY_TYPES.filter(d => !used.value.includes(d)))
    const total = (rows) => (rows ?? []).reduce((sum, r) => sum + (Number(r?.consumed_kwh) || 0), 0)
    function emitProfile(next) { emit('update:modelValue', Object.keys(next).length ? next : undefined) }
    function setRows(day, rows) {
      emitProfile({ ...profile.value, [day]: rows ?? [] })
    }
    function removeDay(day) {
      const { [day]: _gone, ...rest } = profile.value
      emitProfile(rest)
    }
    function addDay(day) {
      if (day) emitProfile({ ...profile.value, [day]: [{ hour: 0, consumed_kwh: 0 }] })
    }
    return { profile, used, free, total, setRows, removeDay, addDay, ROW_FIELDS }
  },
})
</script>

<template>
  <div class="cfg-profile">
    <div v-for="day in used" :key="day" class="cfg-profile-day">
      <div class="cfg-profile-head">
        <span class="cfg-profile-name">{{ day }}</span>
        <span class="cfg-hint">{{ total(profile[day]).toFixed(2) }} kWh per day</span>
        <span class="ev-actions-spacer"></span>
        <button type="button" class="btn btn-danger cfg-mini" @click="removeDay(day)">Remove {{ day }}</button>
      </div>
      <TableEditor :fields="ROW_FIELDS" :model-value="profile[day]" add-label="Add segment" @update:model-value="setRows(day, $event)" />
    </div>
    <select v-if="free.length" class="cfg-profile-add" aria-label="Add a day type" @change="addDay($event.target.value); $event.target.value = ''">
      <option value="">+ Add day type…</option>
      <option v-for="d in free" :key="d" :value="d">{{ d }}</option>
    </select>
    <div class="cfg-hint">weekdays / weekends are the fallback for days without their own profile.</div>
  </div>
</template>

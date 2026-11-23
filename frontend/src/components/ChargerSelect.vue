<script>
import { defineComponent, ref, computed } from 'vue'
import { selectEvCharger } from '../api.js'
import { markPlanning } from '../composables/useStatus.js'

// Which charger a car (or the guest) charges at.  The last selection wins: a
// car plugged in at that charger is reassigned now, a free charger expects
// this car at its next plug-in.  When another car or a guest has taken this
// car's default charger, the selector shows that charger greyed out until it
// is free again (then the default applies by itself); selecting it again
// takes it back.
export default defineComponent({
  name: 'ChargerSelect',
  props: {
    who:      { type: String, required: true },   // vehicle id or "guest"
    status:   { type: Object, default: null },    // charger_status of /api/vehicles (or built for the guest)
    chargers: { type: Array, required: true },    // /api/chargers
  },
  emits: ['refresh'],

  setup(props, { emit }) {
    const busy = ref(false)
    const errMsg = ref(null)
    const state = computed(() => props.status?.state ?? 'none')
    const value = computed(() => props.status?.charger_id ?? 'none')

    const note = computed(() => {
      const s = props.status
      switch (state.value) {
        case 'plugged':  return 'plugged in'
        case 'selected': return 'expected at the next plug-in'
        case 'default':  return 'default'
        case 'taken':    return s.plugged ? `in use by ${s.by_label} until unplugged` : `${s.by_label} is expected there`
        default:         return props.who === 'guest' ? 'not here' : 'no charger'
      }
    })

    async function choose(chargerId) {
      busy.value = true
      errMsg.value = null
      try {
        await selectEvCharger(props.who, chargerId)
        markPlanning()
        emit('refresh')
      } catch (e) {
        errMsg.value = String(e).replace(/^Error: \d+: /, '')
      } finally {
        busy.value = false
      }
    }

    return { busy, errMsg, state, value, note, choose }
  },
})
</script>

<template>
  <div :class="['ev-charger', 'is-' + state]">
    <select class="ev-charger-select" :value="value" :disabled="busy"
            :aria-label="'Charger for ' + (who === 'guest' ? 'a guest car' : who)"
            @change="choose($event.target.value)">
      <option v-for="c in chargers" :key="c.charger_id" :value="c.charger_id">⚡ {{ c.label }}</option>
      <option value="none">— no charger</option>
    </select>
    <span class="ev-charger-note">{{ note }}</span>
    <a v-if="state === 'taken'" href="#" class="ev-charger-note" style="color:var(--primary);font-weight:600"
       @click.prevent="choose(value)">take it back</a>
    <span v-if="errMsg" class="ev-error">{{ errMsg }}</span>
  </div>
</template>

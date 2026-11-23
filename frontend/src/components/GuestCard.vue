<script>
import { defineComponent, ref, computed } from 'vue'
import { setGuestMode } from '../api.js'
import { markPlanning } from '../composables/useStatus.js'
import ChargerSelect from './ChargerSelect.vue'

// A visitor's car: which charger it uses (until it is unplugged) and how it
// charges there: PV surplus only, or full power now.  When the guest leaves,
// the charger's default car and its configured guest mode apply again.
const MODES = [
  { id: 'pv_only', label: '☀ PV', title: 'Charge from PV surplus only' },
  { id: 'instant', label: '⚡ Charge now', title: 'Charge at full power now' },
]

export default defineComponent({
  name: 'GuestCard',
  components: { ChargerSelect },
  props: { chargers: { type: Array, required: true } },   // /api/chargers
  emits: ['refresh'],

  setup(props, { emit }) {
    const busy = ref(false)
    const errMsg = ref(null)

    // The charger the guest is plugged in at, else the one it is expected at.
    const charger = computed(() =>
      props.chargers.find(c => c.session?.guest) ??
      props.chargers.find(c => !c.session && c.next_vehicle === 'guest') ?? null)
    const status = computed(() => !charger.value
      ? { charger_id: null, state: 'none' }
      : { charger_id: charger.value.charger_id, state: charger.value.session ? 'plugged' : 'selected' })
    const mode = computed(() => charger.value?.guest_mode ?? 'pv_only')
    const kw = computed(() => {
      const w = charger.value?.session ? (charger.value.power_w ?? 0) : 0
      return w > 100 ? (w / 1000).toFixed(1) : null
    })

    async function choose(m) {
      if (m === mode.value || !charger.value) return
      busy.value = true
      errMsg.value = null
      try {
        await setGuestMode(m)
        markPlanning()
        emit('refresh')
      } catch (e) {
        errMsg.value = String(e).replace(/^Error: \d+: /, '')
      } finally {
        busy.value = false
      }
    }

    return { MODES, busy, errMsg, charger, status, mode, kw, choose }
  },
})
</script>

<template>
  <div class="guest-card">
    <span class="ev-label">Guest car</span>
    <ChargerSelect who="guest" :status="status" :chargers="chargers" @refresh="$emit('refresh')" />
    <div class="guest-modes" role="group" aria-label="How the guest car charges">
      <button v-for="m in MODES" :key="m.id" type="button" :title="m.title"
              :class="['guest-mode', { 'is-on': mode === m.id }]" :aria-pressed="mode === m.id"
              :disabled="busy || !charger" @click="choose(m.id)">{{ m.label }}</button>
    </div>
    <span v-if="kw" class="ct-kw">{{ kw }} kW</span>
    <span v-if="errMsg" class="ev-error">{{ errMsg }}</span>
  </div>
</template>

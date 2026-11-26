<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { setDeviceControl } from '../api.js'
import { Switch } from '@/components/ui/switch'
import { useProjectedSavings } from '../composables/useProjectedSavings'

// Observe mode (issue #134): whether Nornwatt controls this device or
// only observes it, with a switch to change it and, while observing, what
// control would have saved so far (simulated).
const props = defineProps<{
  deviceId: string
  /** 'observe' | 'active' */
  control: string
  /** The savings group of the device: "batteries", a charger id, a device id. */
  group: string
  /** Label of the controlled device when the page is another one's (the
   *  plug of a thermal load, a car at its charger). */
  via?: string
}>()
const emit = defineEmits<{ changed: [] }>()

const { projected, load, ofGroup } = useProjectedSavings()
onMounted(load)

const observing = computed(() => props.control === 'observe')
const asking = ref<string | null>(null)   // the mode waiting for confirmation
const saving = ref(false)
const errors = ref<string[]>([])

const saving_eur = computed(() => ofGroup(props.group))
const since = computed(() => projected.value?.since
  ? new Date(projected.value.since).toLocaleDateString([], { day: 'numeric', month: 'short' }) : null)
const eur = (v: number) => `${v < -0.005 ? '−' : v > 0.005 ? '+' : ''}${Math.abs(v).toFixed(2)} €`

function ask(on: boolean) {
  errors.value = []
  asking.value = on ? 'active' : 'observe'
}
async function confirm() {
  if (!asking.value) return
  saving.value = true
  const res = await setDeviceControl(props.deviceId, asking.value)
  saving.value = false
  if (!res.ok) { errors.value = res.errors; return }
  asking.value = null
  emit('changed')
}
</script>

<template>
  <div class="full panel device-control" data-testid="device-control">
    <div class="panel-head">
      <h2>Control<template v-if="via"> · {{ via }}</template></h2>
      <span class="pill" :class="observing ? 'pill-off' : 'pill-ok'" data-testid="control-chip">
        {{ observing ? 'Observing' : 'Controlling' }}
      </span>
      <span class="ev-actions-spacer" />
      <label class="flex items-center gap-2 text-sm">
        <Switch
          :model-value="!observing" :disabled="saving || asking != null"
          :aria-label="via ? `Let Nornwatt control ${via}` : 'Let Nornwatt control this device'"
          data-testid="control-switch"
          @update:model-value="ask($event)"
        />
        {{ via ? `Let Nornwatt control ${via}` : 'Let Nornwatt control this device' }}
      </label>
    </div>

    <p v-if="via" class="panel-hint">This device is controlled through {{ via }}; the switch applies to both.</p>
    <p v-if="observing" class="panel-hint">
      Nornwatt plans this device and logs what it would send, but sends it nothing:
      it keeps running on its own logic.
      <template v-if="saving_eur != null">
        With control it would have saved <b data-testid="projected-saving">{{ eur(saving_eur) }}</b>
        since {{ since }} (simulated).
      </template>
    </p>
    <p v-else class="panel-hint">Nornwatt sends this device its commands according to the plan.</p>

    <div v-if="asking" class="confirm" role="alertdialog" data-testid="control-confirm">
      <p v-if="asking === 'active'">
        From the next control step (within a minute) Nornwatt takes over this device and
        sends it commands according to the plan: it may charge, discharge, stop or switch it
        against its own logic. You can switch back to observing at any time.
      </p>
      <p v-else>
        Nornwatt stops sending commands and hands the device back to its own logic right
        away. It keeps planning it and shows what control would have saved.
      </p>
      <ul v-if="errors.length" class="ev-error">
        <li v-for="e in errors" :key="e">{{ e }}</li>
      </ul>
      <div class="flex gap-2">
        <button type="button" class="btn btn-primary" :disabled="saving" data-testid="control-confirm-ok" @click="confirm">
          {{ saving ? 'Saving…' : asking === 'active' ? 'Start controlling' : 'Only observe' }}
        </button>
        <button type="button" class="btn btn-neutral" :disabled="saving" @click="asking = null">Cancel</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.confirm { margin-top: .75rem; display: flex; flex-direction: column; gap: .6rem; font-size: .875rem; }
</style>

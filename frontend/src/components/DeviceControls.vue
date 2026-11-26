<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '../composables/usePowerFlow'
import { computed, inject } from 'vue'
import DeviceControl from './DeviceControl.vue'

// The observe/active switches that concern one device (issue #134): its own
// when Nornwatt commands it, else those of the devices it is
// controlled through — the thermal load a switch executes (the aquarium
// cooler on its plug), the charger a car charges at (``control_targets`` of
// /api/status).
const props = defineProps<{ deviceId: string }>()
const emit = defineEmits<{ changed: [] }>()
const status = inject('status') as Ref<Status | null>

const targets = computed(() => {
  const devices = status.value?.devices ?? []
  const modes = status.value?.control ?? {}
  const self = devices.find(d => d.device_id === props.deviceId)
  return (self?.control_targets ?? [])
    .filter(id => modes[id])
    .map((id) => {
      const target = devices.find(d => d.device_id === id)
      return {
        id,
        control: modes[id],
        // The savings accounting treats all batteries as one storage.
        group: target?.category === 'battery' ? 'batteries' : id,
        via: id === props.deviceId ? undefined : (target?.label ?? id),
      }
    })
})
</script>

<template>
  <DeviceControl
    v-for="t in targets" :key="t.id"
    :device-id="t.id" :control="t.control" :group="t.group" :via="t.via"
    @changed="emit('changed')"
  />
</template>

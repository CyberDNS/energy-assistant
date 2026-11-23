<script setup lang="ts">
import { computed } from 'vue'
import { fmt, fmtPower } from '@/lib/format'

// One device's live state: availability, power, state of charge and, when the
// controller steers it, what it is told to do right now.
interface Setpoint {
  setpoint_w?: number | null
  mode?: string | null
  category?: string | null
  grid_allowed?: boolean | null
  export_allowed?: boolean | null
}
const props = defineProps<{
  device: {
    device_id: string
    label?: string
    category?: string | null
    power_w?: number | null
    soc_pct?: number | null
    available?: boolean
    sp?: Setpoint
  }
}>()

const CHARGING = ['run', 'charge_from_grid', 'charge_from_pv', 'charge_phase2', 'force_charging']
const pill = computed(() => {
  const mode = props.device.sp?.mode ?? ''
  if (CHARGING.includes(mode)) return 'pill pill-ok'
  if (mode === 'discharge' || mode === 'grid_feed_in') return 'pill pill-warn'
  return 'pill pill-off'
})

const setpoint = computed(() => {
  const s = props.device.sp
  if (!s || s.setpoint_w == null) return '—'
  // Charger sentinel values are not real watts: show the charging mode instead.
  if (s.category === 'charger') {
    if (s.setpoint_w > 500) return `${fmt(s.setpoint_w, 0)} W`
    return s.setpoint_w > 0 ? 'PV mode' : 'stopped'
  }
  return `${fmt(s.setpoint_w, 0)} W`
})
</script>

<template>
  <div class="dev-card">
    <div class="dev-card-header">
      <span
        class="status-dot" :class="device.available ? 'on' : 'off'"
        :title="device.available ? 'online' : 'unavailable'"
      />
      <span class="dev-label">{{ device.label ?? device.device_id }}</span>
      <span class="dev-power" :class="{ producing: (device.power_w ?? 0) < 0 }">{{ fmtPower(device.power_w) }}</span>
    </div>
    <div v-if="device.soc_pct != null" class="soc-row">
      <span class="soc-num">{{ fmt(device.soc_pct, 0) }}%</span>
      <div class="soc-bar">
        <div class="soc-fill" :style="{ width: Math.min(Math.max(device.soc_pct, 0), 100) + '%' }" />
      </div>
    </div>
    <div v-if="device.sp" class="dev-sp">
      <span :class="pill">{{ (device.sp.mode ?? '—').replace(/_/g, ' ') }}</span>
      <span class="dev-sp-w">{{ setpoint }}</span>
      <span v-if="device.sp.grid_allowed != null" class="dev-flag" :class="{ off: !device.sp.grid_allowed }">
        grid {{ device.sp.grid_allowed ? '✓' : '✗' }}
      </span>
      <span v-if="device.sp.export_allowed != null" class="dev-flag" :class="{ off: !device.sp.export_allowed }">
        export {{ device.sp.export_allowed ? '✓' : '✗' }}
      </span>
    </div>
    <slot />
  </div>
</template>

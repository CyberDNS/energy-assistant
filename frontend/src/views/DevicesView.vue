<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '../composables/usePowerFlow'
import { ChevronRight } from '@lucide/vue'
import { computed, inject } from 'vue'
import { RouterLink } from 'vue-router'
import { useLiveDevices } from '../composables/useLiveDevices'
import { CATEGORY_GROUP_TITLES, CATEGORY_ORDER } from '../lib/categories'
import { deviceLink } from '../lib/deviceLinks'
import { fmt, fmtPower } from '../lib/format'

// Every device of the house by kind, with its live figures; a row opens the
// device's page.
const status = inject('status') as Ref<Status | null>
const devices = useLiveDevices(status)

const groups = computed(() => {
  const known = CATEGORY_ORDER.filter(c => devices.value.some(d => d.category === c))
  const other = devices.value.filter(d => !CATEGORY_ORDER.includes(d.category ?? ''))
  return [
    ...known.map(c => ({ id: c, title: CATEGORY_GROUP_TITLES[c], devices: devices.value.filter(d => d.category === c) })),
    ...(other.length ? [{ id: 'other', title: 'Other', devices: other }] : []),
  ]
})

const mode = (d: { sp?: Record<string, unknown> }) => (d.sp?.mode ? String(d.sp.mode).replace(/_/g, ' ') : null)
</script>

<template>
  <div>
    <div v-for="g in groups" :key="g.id" class="panel full" :data-testid="'devices-' + g.id">
      <h2>{{ g.title }}</h2>
      <div class="link-list">
        <RouterLink v-for="d in g.devices" :key="d.device_id" :to="deviceLink(d)" class="link-row">
          <span
            class="status-dot" :class="d.available ? 'on' : 'off'"
            :title="d.available ? 'online' : 'not responding'"
          />
          <span class="link-row-main">{{ d.label ?? d.device_id }}</span>
          <span v-if="mode(d)" class="pill pill-off hidden sm:inline">{{ mode(d) }}</span>
          <span v-if="d.soc_pct != null" class="devrow-soc">
            <span class="soc-bar"><span class="soc-fill block" :style="{ width: Math.min(Math.max(d.soc_pct, 0), 100) + '%' }" /></span>
            <span class="link-row-value">{{ fmt(d.soc_pct, 0) }} %</span>
          </span>
          <span class="link-row-value devrow-power" :class="{ ok: (d.power_w ?? 0) < -10 }">{{ fmtPower(d.power_w) }}</span>
          <ChevronRight class="link-row-chevron" />
        </RouterLink>
      </div>
    </div>
    <div v-if="!groups.length" class="text-muted-foreground py-4 text-sm">{{ status ? 'No devices yet.' : 'Loading…' }}</div>
  </div>
</template>

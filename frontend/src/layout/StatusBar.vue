<script setup lang="ts">
import type { Component, Ref } from 'vue'
import type { FlowTone, Status } from '@/composables/usePowerFlow'
import {
  ArrowDownToLine, ArrowUpFromLine, BatteryCharging, BatteryMedium, Car, FlaskConical, House, Sun, UtilityPole,
} from '@lucide/vue'
import { computed, inject, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { usePowerFlow } from '@/composables/usePowerFlow'
import { fmt, fmtAgo, fmtPower, fmtPrice } from '@/lib/format'

// The always-visible bar at the bottom: what the house is doing right now.
const status = inject('status') as Ref<Status | null>
const statusError = inject('statusError') as Ref<string | null>
const { tiles, batterySocPct } = usePowerFlow(status)

interface Item {
  id: string
  icon: Component
  value: string
  label: string
  /** The label on a phone, where there is room for one word. */
  short: string
  tone: FlowTone
  to: string
  /** Shown on phones too; the others need a wider bar. */
  primary: boolean
}

const TONE: Record<FlowTone, string> = {
  ok: 'text-success', bad: 'text-danger', accent: 'text-info', muted: 'text-muted-foreground',
}

const items = computed<Item[]>(() => tiles.value.map((t): Item => {
  switch (t.id) {
    case 'solar':
      return { id: t.id, icon: Sun, value: fmtPower(t.w), label: `Solar · ${t.dir}`, short: 'Solar', tone: t.tone === 'ok' ? 'ok' : 'muted', to: '/', primary: true }
    case 'grid':
      return { id: t.id, icon: UtilityPole, value: fmtPower(t.w), label: `Grid · ${t.dir}`, short: t.dir === 'idle' ? 'Grid' : t.dir, tone: t.tone, to: '/', primary: true }
    case 'battery': {
      const soc = batterySocPct.value
      return {
        id: t.id, icon: t.dir === 'charging' ? BatteryCharging : BatteryMedium,
        value: soc != null ? `${fmt(soc, 0)} %` : fmtPower(t.w),
        label: t.dir === 'idle' ? 'Battery · idle' : `Battery · ${t.dir}${soc != null ? ` ${fmtPower(t.w)}` : ''}`,
        short: t.dir === 'idle' ? 'Battery' : t.dir, tone: t.tone, to: '/batteries', primary: true,
      }
    }
    case 'ev':
      return { id: t.id, icon: Car, value: fmtPower(t.w), label: `Cars · ${t.dir}`, short: 'Cars', tone: t.tone, to: '/cars', primary: false }
    default:
      return { id: t.id, icon: House, value: fmtPower(t.w), label: 'Home · consuming', short: 'Home', tone: 'muted', to: '/', primary: false }
  }
}))

const importPrice = computed(() => status.value?.current_price_eur_per_kwh)
const exportPrice = computed(() => status.value?.pv_opportunity_price_eur_per_kwh)

// Freshness of the live data.
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 5000) })
onUnmounted(() => clearInterval(timer))
const ago = computed(() => fmtAgo(status.value?.timestamp, now.value))
const stale = computed(() => !!statusError.value
  || (status.value != null && now.value - new Date(status.value.timestamp).getTime() > 90_000))
</script>

<template>
  <footer
    class="bg-sidebar/95 fixed inset-x-0 bottom-0 z-30 h-[var(--statusbar-h)] border-t pb-[var(--device-inset-bottom)] backdrop-blur"
    aria-label="Live status" data-testid="status-bar"
  >
    <div
      class="flex h-full items-center gap-1 overflow-x-auto pr-[calc(var(--device-inset-right)+0.5rem)] pl-[calc(var(--device-inset-left)+0.5rem)] md:gap-2 md:px-4"
    >
      <template v-if="items.length">
        <RouterLink
          v-for="item in items" :key="item.id" :to="item.to" :data-testid="'status-' + item.id"
          class="hover:bg-sidebar-accent shrink-0 items-center gap-2 rounded-lg px-1.5 py-1.5 transition-colors sm:gap-2.5 sm:px-2.5"
          :class="item.primary ? 'flex' : 'hidden sm:flex'"
        >
          <component :is="item.icon" class="size-5 shrink-0" :class="TONE[item.tone]" />
          <span class="flex flex-col leading-tight">
            <span class="text-[0.95rem] font-semibold tabular-nums">{{ item.value }}</span>
            <span class="text-muted-foreground hidden text-[11px] whitespace-nowrap sm:inline">{{ item.label }}</span>
            <span class="text-muted-foreground text-[11px] whitespace-nowrap sm:hidden">{{ item.short }}</span>
          </span>
        </RouterLink>
      </template>
      <span v-else class="text-muted-foreground px-2 text-sm">{{ statusError ? 'No live data' : 'Loading…' }}</span>

      <span class="min-w-1 flex-1" />

      <RouterLink
        v-if="importPrice != null" to="/plan" data-testid="status-price"
        class="hover:bg-sidebar-accent flex shrink-0 items-center gap-2 rounded-lg px-1.5 py-1.5 transition-colors sm:gap-2.5 sm:px-2.5"
      >
        <ArrowDownToLine class="text-muted-foreground hidden size-5 shrink-0 sm:block" />
        <span class="flex flex-col leading-tight">
          <span class="text-[0.95rem] font-semibold tabular-nums">{{ fmtPrice(importPrice) }}</span>
          <span class="text-muted-foreground text-[11px] whitespace-nowrap"><span class="hidden sm:inline">Import price</span><span class="sm:hidden">Price</span> / kWh</span>
        </span>
      </RouterLink>
      <RouterLink
        v-if="exportPrice != null" to="/plan"
        class="hover:bg-sidebar-accent hidden shrink-0 items-center gap-2.5 rounded-lg px-2.5 py-1.5 transition-colors lg:flex"
      >
        <ArrowUpFromLine class="text-muted-foreground size-5 shrink-0" />
        <span class="flex flex-col leading-tight">
          <span class="text-[0.95rem] font-semibold tabular-nums">{{ fmtPrice(exportPrice) }}</span>
          <span class="text-muted-foreground text-[11px] whitespace-nowrap">Export price / kWh</span>
        </span>
      </RouterLink>

      <RouterLink
        v-if="status?.dry_run" to="/settings/system" data-testid="status-dry-run"
        class="bg-warning-soft text-warning flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold"
        title="Dry run: devices are not being controlled"
      >
        <FlaskConical class="size-3.5" /> Dry run
      </RouterLink>

      <span
        class="text-muted-foreground hidden shrink-0 items-center gap-1.5 pr-1 pl-2 text-xs whitespace-nowrap md:flex"
        :title="statusError ?? undefined"
      >
        <span class="size-2 rounded-full" :class="stale ? 'bg-danger' : 'bg-success animate-pulse'" />
        {{ statusError ? 'offline' : ago ? `updated ${ago}` : 'connecting…' }}
      </span>
    </div>
  </footer>
</template>

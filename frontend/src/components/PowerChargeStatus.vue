<script setup lang="ts">
import type { PowerChargeStatus } from '../composables/usePowerFlow'
import { computed } from 'vue'
import { fmt } from '../lib/format'

// Power-dependent grid charges (threshold / month peak): the energy drawn in
// the running 15-minute interval against what it may draw without a charge,
// and this month's peak.
const props = defineProps<{ pc: PowerChargeStatus }>()

const INTERVAL_H = 0.25
const kw = (v: number | null | undefined) => (v == null ? '—' : `${fmt(v, 2)} kW`)
const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const day = (iso: string) => new Date(iso).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })

const ends = computed(() => time(new Date(new Date(props.pc.interval_start).getTime() + INTERVAL_H * 3600_000).toISOString()))
// Share of the interval's charge-free energy already drawn.
const used = computed(() => {
  const free = props.pc.free_kw
  if (free == null || free <= 0) return null
  return props.pc.interval_import_kwh / (free * INTERVAL_H)
})
const tone = computed(() => (used.value == null ? '' : used.value > 1 ? 'bad' : used.value > 0.85 ? 'warn' : ''))
const curtailed = computed(() => Object.keys(props.pc.curtailed ?? {}))
const minimum = computed(() => Math.max(0, ...props.pc.peaks.map(p => p.minimum_kw)))
const peakNote = computed(() => [
  props.pc.month_peak_at ? day(props.pc.month_peak_at) : null,
  minimum.value > 0 ? `billed at least ${kw(minimum.value)}` : null,
].filter(Boolean).join(' · '))
</script>

<template>
  <section class="panel full" aria-label="Power charges" data-testid="power-charges">
    <div class="panel-head">
      <h2>Power charges</h2>
      <span class="link-row-sub" style="white-space:normal">15-minute average import at the grid connection</span>
    </div>
    <dl class="pc-figures">
      <div>
        <dt>This interval (until {{ ends }})</dt>
        <dd>{{ kw(pc.interval_average_kw) }}</dd>
        <div v-if="used != null" class="soc-row pc-bar">
          <div class="soc-bar"><div class="soc-fill" :class="tone" :style="{ width: Math.min(100, used * 100) + '%' }" /></div>
          <span class="soc-num">{{ fmt(used * 100, 0) }} %</span>
        </div>
        <span class="link-row-sub">{{ fmt(pc.interval_import_kwh, 2) }} kWh of {{ fmt((pc.free_kw ?? 0) * INTERVAL_H, 2) }} kWh without charge</span>
      </div>
      <div>
        <dt>Without charge up to</dt>
        <dd>{{ kw(pc.free_kw) }}</dd>
        <span v-if="pc.planned_import_kw != null" class="link-row-sub">plan for this slot {{ kw(pc.planned_import_kw) }}</span>
      </div>
      <div v-for="(t, i) in pc.thresholds" :key="'t' + i">
        <dt>Threshold</dt>
        <dd>{{ kw(t.threshold_kw) }}</dd>
        <span class="link-row-sub">+{{ fmt(t.excess_eur_per_kwh * 100, 1) }} ct per kWh above</span>
      </div>
      <div v-if="pc.peaks.length">
        <dt>Peak this month</dt>
        <dd>{{ kw(pc.month_peak_kw) }}</dd>
        <span class="link-row-sub">{{ peakNote }}</span>
      </div>
    </dl>
    <p v-if="curtailed.length" class="link-row-sub pc-note" data-testid="power-charges-curtailed">
      Held back to stay below it: {{ curtailed.join(', ') }}
    </p>
  </section>
</template>

<style scoped>
.pc-figures { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px 18px; }
.pc-figures dt { color: var(--muted-foreground); font-size: .76rem; }
.pc-figures dd { font-size: 1.25rem; font-weight: 650; letter-spacing: -.01em; font-variant-numeric: tabular-nums; }
.pc-bar { margin: 4px 0 2px; }
.soc-fill.warn { background: var(--warning, #d99a00); }
.soc-fill.bad { background: var(--destructive, #c2553f); }
.pc-note { margin-top: 10px; }
</style>

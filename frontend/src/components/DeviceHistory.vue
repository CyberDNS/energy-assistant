<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchHistory } from '../api.js'
import { fmt } from '../lib/format'
import { plotTime } from '../lib/plotTime'
import PlotlyChart from './PlotlyChart.js'
import RangeButtons from './RangeButtons.vue'

// The measured history of one device: power, and state of charge where the
// device has one.  Short ranges come in 5-minute steps, long ones per hour.
const props = defineProps<{
  deviceId: string
  /** Where the power is measured, when that is another device (a car's charger). */
  powerDeviceId?: string | null
  category?: string | null
}>()

interface Row { t: string, power_w: number | null, soc_pct: number | null, pos_w: number | null, neg_w: number | null }
interface Range { label: string, hours: number, tickformat: string }

const RANGES: Range[] = [
  { label: '24 h', hours: 24, tickformat: '%H:%M' },
  { label: '3 d', hours: 72, tickformat: '%d.%m. %H:%M' },
  { label: '14 d', hours: 336, tickformat: '%d.%m.' },
  { label: '90 d', hours: 2160, tickformat: '%d.%m.' },
  { label: '1 y', hours: 8760, tickformat: '%b %y' },
]

const range = ref<Range>(RANGES[0])
const pending = ref<Range | null>(null)
const power = ref<Row[]>([])
const soc = ref<Row[]>([])
const bucketMinutes = ref(5)
const loaded = ref(false)
let request = 0
let timer: ReturnType<typeof setInterval> | undefined

async function load(r: Range = range.value) {
  const mine = ++request
  pending.value = r
  const powerId = props.powerDeviceId ?? props.deviceId
  try {
    const resp = await fetchHistory(r.hours, [...new Set([props.deviceId, powerId])])
    if (mine !== request) return // a newer click won
    power.value = resp.measurements?.[powerId] ?? []
    soc.value = resp.measurements?.[props.deviceId] ?? []
    bucketMinutes.value = resp.bucket_minutes ?? 5
    range.value = r
  } catch {
    /* keep what is shown */
  }
  if (mine === request) pending.value = null
  loaded.value = true
}
onMounted(() => { load(); timer = setInterval(() => load(), 5 * 60_000) })
onUnmounted(() => clearInterval(timer))
watch(() => [props.deviceId, props.powerDeviceId], () => load())

const hasPower = computed(() => power.value.some(r => r.power_w != null))
const hasSoc = computed(() => soc.value.some(r => r.soc_pct != null))

// Words for the two directions of energy, by what the device is.
const WORDS: Record<string, [string | null, string | null]> = {
  pv: [null, 'Produced'],
  battery: ['Charged', 'Discharged'],
  meter: ['Imported', 'Exported'],
  charger: ['Charged', null],
  vehicle: ['Charged', null],
}
const energy = computed(() => {
  const hours = bucketMinutes.value / 60
  const kwh = (key: 'pos_w' | 'neg_w') => power.value.reduce((a, r) => a + (r[key] ?? 0), 0) * hours / 1000
  const [into, out] = WORDS[props.category ?? ''] ?? ['Consumed', null]
  const parts: { label: string, kwh: number }[] = []
  if (into) parts.push({ label: into, kwh: kwh('pos_w') })
  if (out) parts.push({ label: out, kwh: kwh('neg_w') })
  return parts
})

const traces = computed(() => {
  const out: Record<string, unknown>[] = []
  if (hasPower.value) {
    const rows = power.value.filter(r => r.power_w != null)
    out.push({
      name: 'Power', mode: 'lines', x: rows.map(r => plotTime(r.t)), y: rows.map(r => (r.power_w as number) / 1000),
      line: { width: 1.6, color: '#2a78d6', shape: 'hv' }, fill: 'tozeroy', fillcolor: 'rgba(42,120,214,.14)',
      hovertemplate: '%{x|%d.%m. %H:%M}  %{y:.2f} kW<extra>Power</extra>',
    })
  }
  if (hasSoc.value) {
    const rows = soc.value.filter(r => r.soc_pct != null)
    out.push({
      name: 'State of charge', mode: 'lines', x: rows.map(r => plotTime(r.t)), y: rows.map(r => r.soc_pct),
      yaxis: hasPower.value ? 'y2' : 'y', line: { width: 2, color: '#6250d6' },
      hovertemplate: '%{x|%d.%m. %H:%M}  %{y:.0f} %<extra>State of charge</extra>',
    })
  }
  return out
})

const layout = computed(() => {
  const socAxis = { title: '%', range: [0, 105] }
  return {
    xaxis: { type: 'date', tickformat: range.value.tickformat },
    yaxis: hasPower.value ? { title: 'kW', zeroline: true } : socAxis,
    ...(hasPower.value && hasSoc.value
      ? { yaxis2: { ...socAxis, overlaying: 'y', side: 'right', showgrid: false }, margin: { t: 10, r: 45, b: 60, l: 50 } }
      : {}),
    showlegend: hasPower.value && hasSoc.value,
  }
})
</script>

<template>
  <div class="full panel" data-testid="device-history">
    <div class="panel-head">
      <h2>History</h2>
      <span v-for="e in energy" v-show="hasPower" :key="e.label" class="link-row-sub">
        {{ e.label }} <b class="text-foreground tabular-nums">{{ fmt(e.kwh, e.kwh < 10 ? 2 : 1) }} kWh</b>
      </span>
      <span class="ev-actions-spacer" />
      <RangeButtons :ranges="RANGES" :active="pending ?? range" @pick="load" />
    </div>
    <PlotlyChart v-if="traces.length" :traces="traces" :layout="layout" height="280px" />
    <p v-else class="text-muted-foreground py-6 text-sm">
      {{ loaded ? 'Nothing recorded in this range yet.' : 'Loading…' }}
    </p>
    <p class="chart-hint">
      {{ bucketMinutes === 5 ? '5-minute averages' : 'Hourly averages' }}. Drag to move in time, pinch or
      Ctrl + scroll to zoom, double-click to see everything.
    </p>
  </div>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import type { FlowTile } from '../composables/usePowerFlow'
import { BatteryMedium, Car, House, Sun, UtilityPole } from '@lucide/vue'
import { useMediaQuery } from '@vueuse/core'
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { IDLE_W } from '../composables/usePowerFlow'
import { fmt, fmtPower } from '../lib/format'

// The house's power flow as a picture: every source and sink around a hub,
// with moving dashes on each link showing which way the power goes.
const props = defineProps<{
  tiles: FlowTile[]
  batterySocPct: number | null
}>()

interface Node {
  id: FlowTile['id']
  icon: Component
  x: number
  y: number
  to: string
  color: string
}
type Place = Omit<Node, 'id' | 'x' | 'y'>
const NODES: Record<FlowTile['id'], Place> = {
  solar: { icon: Sun, to: '/devices', color: 'var(--flow-solar)' },
  grid: { icon: UtilityPole, to: '/devices', color: 'var(--flow-grid)' },
  home: { icon: House, to: '/devices', color: 'var(--flow-home)' },
  battery: { icon: BatteryMedium, to: '/batteries', color: 'var(--flow-battery)' },
  ev: { icon: Car, to: '/cars', color: 'var(--flow-car)' },
}
// Two drawings: a wide one, and a narrow one for phones in which the same
// circles and text take up more of the width.
interface Drawing { w: number, h: number, hub: [number, number], at: Record<FlowTile['id'], [number, number]> }
const WIDE: Drawing = {
  w: 640, h: 352, hub: [320, 168],
  at: { solar: [320, 52], grid: [96, 168], home: [544, 168], battery: [196, 278], ev: [444, 278] },
}
const NARROW: Drawing = {
  w: 360, h: 352, hub: [180, 168],
  at: { solar: [180, 50], grid: [52, 168], home: [308, 168], battery: [92, 278], ev: [268, 278] },
}
const narrow = useMediaQuery('(max-width: 640px)')
const drawing = computed(() => (narrow.value ? NARROW : WIDE))
const R = 34

const nodes = computed(() => props.tiles.map((t) => {
  const [x, y] = drawing.value.at[t.id]
  const [hx, hy] = drawing.value.hub
  const pos = { ...NODES[t.id], x, y }
  const active = t.w > IDLE_W && t.dir !== 'idle'
  // Into the hub: production, import, discharging.  Out of it: everything else.
  const inward = t.dir === 'producing' || t.dir === 'importing' || t.dir === 'discharging'
  // Shorten the link so it starts at the circle's edge.
  const dx = hx - pos.x, dy = hy - pos.y
  const len = Math.hypot(dx, dy)
  const sx = pos.x + dx / len * (R + 4), sy = pos.y + dy / len * (R + 4)
  return {
    ...t, ...pos, active, inward,
    path: `M${sx} ${sy} L${hx} ${hy}`,
    // Faster dashes for more power, within reason.
    seconds: Math.max(0.6, 2.4 - Math.log10(Math.max(t.w, 10)) * 0.5),
    value: t.id === 'battery' && props.batterySocPct != null ? `${fmt(props.batterySocPct, 0)} %` : fmtPower(t.w),
    sub: t.id === 'battery' && props.batterySocPct != null && active ? `${t.dir} ${fmtPower(t.w)}` : t.dir,
  }
}))
</script>

<template>
  <div class="flow" data-testid="energy-flow">
    <svg :viewBox="`0 0 ${drawing.w} ${drawing.h}`" role="img" aria-label="Power flow of the house">
      <g fill="none" stroke-linecap="round">
        <template v-for="n in nodes" :key="n.id">
          <path :d="n.path" class="flow-track" />
          <path
            v-if="n.active" :d="n.path" class="flow-dash" :class="n.inward ? 'in' : 'out'"
            :style="{ stroke: n.color, animationDuration: n.seconds + 's' }"
          />
        </template>
      </g>
      <circle :cx="drawing.hub[0]" :cy="drawing.hub[1]" r="7" class="flow-hub" />
      <RouterLink v-for="n in nodes" :key="n.id" :to="n.to" custom v-slot="{ navigate, href }">
        <a :href="href" class="flow-node" :aria-label="`${n.label}: ${n.value}, ${n.sub}`" @click="navigate">
          <circle :cx="n.x" :cy="n.y" :r="R" class="flow-circle" :style="{ stroke: n.active ? n.color : undefined }" />
          <component :is="n.icon" :x="n.x - 11" :y="n.y - 24" width="22" height="22" :style="{ color: n.color }" />
          <text :x="n.x" :y="n.y + 14" class="flow-value">{{ n.value }}</text>
          <!-- Solar sits above the hub: its label goes beside it, clear of the link. -->
          <template v-if="n.id === 'solar'">
            <text :x="n.x + R + 10" :y="n.y - 3" class="flow-label side">{{ n.label }}</text>
            <text :x="n.x + R + 10" :y="n.y + 12" class="flow-label side">{{ n.sub }}</text>
          </template>
          <template v-else>
            <text :x="n.x" :y="n.y + R + 16" class="flow-label">{{ n.label }}</text>
            <text :x="n.x" :y="n.y + R + 30" class="flow-label">{{ n.sub }}</text>
          </template>
        </a>
      </RouterLink>
    </svg>
  </div>
</template>

<style scoped>
.flow { --flow-solar: var(--brand-bolt); --flow-grid: var(--series-import); --flow-home: var(--series-home); --flow-battery: var(--series-battery); --flow-car: var(--series-car); }
.flow svg { display: block; width: 100%; max-width: 640px; height: auto; margin: 0 auto; }
.flow-track { stroke: var(--border); stroke-width: 3; }
.flow-dash { stroke-width: 3.5; stroke-dasharray: 2 11; animation: flow-in linear infinite; }
.flow-dash.out { animation-name: flow-out; }
.flow-hub { fill: var(--card); stroke: var(--border); stroke-width: 3; }
.flow-circle { fill: var(--card); stroke: var(--border); stroke-width: 2.5; transition: stroke .3s; }
.flow-node:hover .flow-circle, .flow-node:focus-visible .flow-circle { fill: var(--accent); }
.flow-node { outline: none; }
.flow-value { fill: var(--foreground); font-size: 13px; font-weight: 650; text-anchor: middle; font-variant-numeric: tabular-nums; }
.flow-label { fill: var(--muted-foreground); font-size: 11.5px; text-anchor: middle; }
.flow-label.side { text-anchor: start; }
@keyframes flow-in { to { stroke-dashoffset: -26; } }
@keyframes flow-out { to { stroke-dashoffset: 26; } }
@media (prefers-reduced-motion: reduce) { .flow-dash { animation: none; } }
</style>

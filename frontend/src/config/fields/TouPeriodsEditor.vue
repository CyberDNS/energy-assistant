<script>
import { defineComponent, computed } from 'vue'
import { buildWeek, runs, weekProblems, formatQuarter, DAY_NAMES } from './tou.js'

// The periods of a time-of-use tariff: a card per period (CardListEditor)
// and below them the resulting week, one bar per weekday coloured by period,
// with the gaps and overlaps spelled out.
export default defineComponent({
  name: 'TouPeriodsEditor',
  props: {
    spec:       { type: Object, required: true },
    modelValue: { default: undefined },
    selfId:     { type: String, default: '' },
  },
  emits: ['update:modelValue'],
  setup(props) {
    const periods = computed(() => (Array.isArray(props.modelValue) ? props.modelValue : []))
    const week = computed(() => buildWeek(periods.value))
    const problems = computed(() => weekProblems(week.value))
    const days = computed(() => week.value.table.map((day, d) => ({
      name: DAY_NAMES[d],
      runs: runs(day).map(r => ({ ...r, left: (r.start / 96) * 100, width: ((r.end - r.start) / 96) * 100 })),
    })))
    const priceOf = (i) => {
      const p = periods.value[i]?.price_eur_per_kwh
      return typeof p === 'number' ? `${p} €/kWh` : 'no price'
    }
    // Categorical slots in fixed order (see styles: --tou-1 … --tou-8); a 9th
    // period and beyond repeat nothing: they are shown in neutral grey.
    const colorClass = (cell) => (cell === null ? 'is-gap' : cell === -1 ? 'is-overlap'
      : cell < 8 ? `tou-${cell + 1}` : 'tou-other')
    const label = (day, r) => {
      const range = `${day.name} ${formatQuarter(r.start)}–${formatQuarter(r.end)}`
      if (r.cell === null) return `${range}: no price`
      if (r.cell === -1) return `${range}: overlapping windows`
      return `${range}: ${week.value.names[r.cell]}, ${priceOf(r.cell)}`
    }
    return { periods, week, problems, days, priceOf, colorClass, label }
  },
})
</script>

<template>
  <div class="cfg-tou">
    <CardListEditor :spec="spec" :model-value="modelValue" :self-id="selfId" @update:model-value="$emit('update:modelValue', $event)" />
    <figure v-if="periods.length" class="cfg-tou-week" aria-label="Resulting week">
      <figcaption class="cfg-name">Resulting week</figcaption>
      <div class="cfg-tou-grid">
        <span></span>
        <div class="cfg-tou-ticks" aria-hidden="true"><span v-for="h in [0, 6, 12, 18, 24]" :key="h" :style="{ left: (h / 24 * 100) + '%' }">{{ String(h).padStart(2, '0') }}</span></div>
        <template v-for="day in days" :key="day.name">
          <span class="cfg-tou-day">{{ day.name }}</span>
          <div class="cfg-tou-bar">
            <span v-for="r in day.runs" :key="r.start" :class="['cfg-tou-seg', colorClass(r.cell)]"
                  :style="{ left: r.left + '%', width: r.width + '%' }" :title="label(day, r)" :aria-label="label(day, r)" role="img"></span>
          </div>
        </template>
      </div>
      <ul class="cfg-tou-legend">
        <li v-for="(name, i) in week.names" :key="i"><span :class="['cfg-tou-swatch', colorClass(i)]"></span>{{ name }} <span class="cfg-hint">{{ priceOf(i) }}<template v-if="week.defaults.includes(i)"> · all other times</template></span></li>
        <li v-if="problems.length"><span class="cfg-tou-swatch is-gap"></span>no price / <span class="cfg-tou-swatch is-overlap"></span>overlap</li>
      </ul>
      <ul v-if="problems.length" class="cfg-tou-problems" role="status">
        <li v-for="p in problems" :key="p">⚠ {{ p }}</li>
      </ul>
      <div v-else class="cfg-hint">Every quarter hour of the week has one price. Seasons only change prices, not times.</div>
    </figure>
  </div>
</template>

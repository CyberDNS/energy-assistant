import { defineComponent, computed, h, onMounted, onUnmounted, ref, watch } from 'vue'

const PLT_OPT = { displayModeBar: false, responsive: true }

function toPlotlyDate(iso) {
  const d = new Date(iso)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:00`
}

function fmt(v, dec = 1) {
  return v == null ? '—' : Number(v).toFixed(dec)
}

const MODE_COLOR = {
  'run':              '#4caf7d',
  'charge_from_pv':   '#f0c040',
  'charge_from_grid': '#e07070',
  'discharge':        '#4caf7d',
  'grid_feed_in':     '#3a9ad9',
  'idle':             'rgba(130,130,130,0.18)',
  'standby':          'rgba(130,130,130,0.18)',
}

function buildPowerTraces(device) {
  const steps = device.plan?.steps ?? []
  if (!steps.length) return { traces: [] }

  const xs     = steps.map(s => toPlotlyDate(s.ts))
  const ys     = steps.map(s => s.planned_kw ?? 0)
  const colors = steps.map(s => MODE_COLOR[s.mode] ?? 'rgba(130,130,130,0.18)')

  return {
    traces: [{
      type: 'bar',
      x: xs,
      y: ys,
      marker: { color: colors },
      hovertemplate: '%{x|%H:%M}  %{y:.3f} kW<extra></extra>',
    }],
  }
}

function miniNticks(widthPx) {
  return Math.min(12, Math.max(2, Math.floor(widthPx / 55)))
}

const MiniChart = defineComponent({
  props: {
    traces:  { type: Array,  default: () => [] },
    layout:  { type: Object, default: () => ({}) },
  },
  setup(props) {
    const el = ref(null)
    let initialized = false
    let ro = null

    function buildLayout() {
      const width = el.value?.offsetWidth ?? 300
      return {
        margin: { t: 4, r: 10, b: 36, l: 50 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { size: 9, color: '#586660' },
        showlegend: false,
        xaxis: {
          type: 'date',
          tickformat: '%H',
          tickangle: 0,
          nticks: miniNticks(width),
          minor: { dtick: 900000, ticks: 'outside', ticklen: 3 },
        },
        ...props.layout,
      }
    }

    function draw() {
      if (!el.value || !props.traces.length) return
      if (initialized) {
        Plotly.react(el.value, props.traces, buildLayout(), PLT_OPT)
      } else {
        Plotly.newPlot(el.value, props.traces, buildLayout(), PLT_OPT)
        initialized = true
      }
    }

    watch(() => [props.traces, props.layout], draw, { deep: true })

    onMounted(() => {
      draw()
      ro = new ResizeObserver(entries => {
        const w = entries[0]?.contentRect.width
        if (w && initialized) Plotly.relayout(el.value, { 'xaxis.nticks': miniNticks(w) })
      })
      ro.observe(el.value)
    })

    onUnmounted(() => {
      ro?.disconnect()
      if (el.value) Plotly.purge(el.value)
    })

    return () => h('div', { ref: el, style: { height: '120px', minWidth: 0 } })
  },
})

export default defineComponent({
  name: 'ControllableDeviceRow',
  components: { MiniChart },
  props: { device: { type: Object, required: true } },

  setup(props) {
    const chart = computed(() => buildPowerTraces(props.device))

    const chartLayout = computed(() => ({
      yaxis: { title: 'kW' },
      bargap: 0.05,
    }))

    const modePillClass = computed(() => {
      const m = props.device.plan?.current_mode ?? ''
      if (m === 'run' || m === 'charge_from_grid' || m === 'charge_from_pv') return 'pill pill-ok'
      if (m === 'discharge' || m === 'grid_feed_in') return 'pill pill-warn'
      return 'pill pill-off'
    })

    const modeLabel = computed(() =>
      (props.device.plan?.current_mode ?? 'unknown').replace(/_/g, ' ')
    )

    const liveValue = computed(() => {
      if (props.device.type === 'threshold') {
        const v = props.device.current_value
        return v != null ? `${fmt(v)} ${props.device.unit}` : '—'
      }
      const soc = props.device.soc_pct
      const pw  = props.device.power_w
      const socStr = soc != null ? `${fmt(soc, 0)}%` : '—'
      const kwStr  = pw  != null ? ` ${pw >= 0 ? '+' : ''}${fmt(pw/1000, 2)} kW` : ''
      return socStr + kwStr
    })

    const nextChange = computed(() => {
      const nc = props.device.plan?.next_change
      if (!nc) return null
      const m = nc.in_minutes
      return `→ ${nc.mode.replace(/_/g, ' ')} in ${m < 60 ? m + ' min' : fmt(m / 60, 1) + ' h'}`
    })

    return { chart, chartLayout, modePillClass, modeLabel, liveValue, nextChange }
  },

  template: `
    <div class="ctrl-row">
      <div class="ctrl-row-header">
        <span class="ctrl-label">{{ device.label }}</span>
        <span class="ctrl-live">{{ liveValue }}</span>
        <span :class="modePillClass">{{ modeLabel }}</span>
        <span v-if="nextChange" class="ctrl-next">{{ nextChange }}</span>
      </div>
      <MiniChart :traces="chart.traces" :layout="chartLayout" />
    </div>
  `,
})

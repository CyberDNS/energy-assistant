import { defineComponent, ref, watch, onMounted, onUnmounted, h } from 'vue'
import Plotly from 'plotly.js-basic-dist-min'
import { baseLayout, themeAxes } from '../lib/chartTheme.ts'
import { isDark } from '../composables/useTheme.ts'

const PLT_OPT = { displayModeBar: false, responsive: true }

function mkLayout(extra = {}) {
  return themeAxes({
    margin: { t: 10, r: 10, b: 60, l: 50 },
    ...baseLayout(11),
    legend: { orientation: 'h', y: -0.32, font: { size: 10 } },
    barmode: extra.barmode,
    ...extra,
  })
}

function xNticks(widthPx) {
  // ~55 px per hour label; at least 3, at most 24
  return Math.min(24, Math.max(3, Math.floor(widthPx / 55)))
}

export default defineComponent({
  name: 'PlotlyChart',
  props: {
    traces:  { type: Array,  default: () => [] },
    layout:  { type: Object, default: () => ({}) },
    height:  { type: String, default: '260px' },
    barmode: { type: String, default: undefined },
  },
  setup(props) {
    const el = ref(null)
    let initialized = false
    let ro = null
    let axisKey = null

    function buildLayout() {
      const width = el.value?.offsetWidth ?? 600
      // A fixed tick step (dtick) wins over the width-based tick count.
      const xaxis = props.layout.xaxis?.dtick
        ? { ...props.layout.xaxis }
        : { ...props.layout.xaxis, nticks: xNticks(width) }
      return mkLayout({ ...props.layout, xaxis, ...(props.barmode ? { barmode: props.barmode } : {}) })
    }

    // x-axis settings whose change makes Plotly.react extremely slow on a
    // date axis (seconds even for a handful of points) — redraw from
    // scratch instead, which takes milliseconds.
    function xAxisKey() {
      const x = props.layout.xaxis ?? {}
      return JSON.stringify([x.type, x.dtick, x.tick0, x.tickformat, x.tickmode])
    }

    function draw() {
      if (!el.value || !props.traces.length) return
      const layout = buildLayout()
      const key = xAxisKey()
      if (initialized && key !== axisKey) {
        Plotly.purge(el.value)
        initialized = false
      }
      axisKey = key
      if (initialized) {
        Plotly.react(el.value, props.traces, layout, PLT_OPT)
      } else {
        Plotly.newPlot(el.value, props.traces, layout, PLT_OPT)
        initialized = true
      }
    }

    watch(() => [props.traces, props.layout], draw, { deep: true })
    // Text and grid colours come from the theme.
    watch(isDark, () => requestAnimationFrame(draw))

    onMounted(() => {
      draw()
      ro = new ResizeObserver(entries => {
        const w = entries[0]?.contentRect.width
        if (w && initialized && !props.layout.xaxis?.dtick) {
          Plotly.relayout(el.value, { 'xaxis.nticks': xNticks(w) })
        }
      })
      ro.observe(el.value)
    })

    onUnmounted(() => {
      ro?.disconnect()
      if (el.value) Plotly.purge(el.value)
    })

    return () => h('div', { ref: el, style: { height: props.height } })
  },
})

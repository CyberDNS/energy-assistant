import { defineComponent, ref, watch, onMounted, onUnmounted, h } from 'vue'

const PLT_OPT = { displayModeBar: false, responsive: true }

function mkLayout(extra = {}) {
  return {
    margin: { t: 10, r: 10, b: 60, l: 50 },
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: { size: 11, color: '#586660' },
    legend: { orientation: 'h', y: -0.32, font: { size: 10 } },
    barmode: extra.barmode,
    ...extra,
  }
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

    function buildLayout() {
      const width = el.value?.offsetWidth ?? 600
      const xaxis = { ...props.layout.xaxis, nticks: xNticks(width) }
      return mkLayout({ ...props.layout, xaxis, ...(props.barmode ? { barmode: props.barmode } : {}) })
    }

    function draw() {
      if (!el.value || !props.traces.length) return
      const layout = buildLayout()
      if (initialized) {
        Plotly.react(el.value, props.traces, layout, PLT_OPT)
      } else {
        Plotly.newPlot(el.value, props.traces, layout, PLT_OPT)
        initialized = true
      }
    }

    watch(() => [props.traces, props.layout], draw, { deep: true })

    onMounted(() => {
      draw()
      ro = new ResizeObserver(entries => {
        const w = entries[0]?.contentRect.width
        if (w && initialized) Plotly.relayout(el.value, { 'xaxis.nticks': xNticks(w) })
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

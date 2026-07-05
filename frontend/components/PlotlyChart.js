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

    function draw() {
      if (!el.value || !props.traces.length) return
      const layout = mkLayout({ ...props.layout, ...(props.barmode ? { barmode: props.barmode } : {}) })
      if (initialized) {
        Plotly.react(el.value, props.traces, layout, PLT_OPT)
      } else {
        Plotly.newPlot(el.value, props.traces, layout, PLT_OPT)
        initialized = true
      }
    }

    watch(() => [props.traces, props.layout], draw, { deep: true })
    onMounted(draw)
    onUnmounted(() => { if (el.value) Plotly.purge(el.value) })

    return () => h('div', { ref: el, style: { height: props.height } })
  },
})

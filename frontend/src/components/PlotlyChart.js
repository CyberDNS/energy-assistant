import { defineComponent, ref, watch, onMounted, onUnmounted, h } from 'vue'
import Plotly from 'plotly.js-basic-dist-min'
import { baseLayout, themeAxes } from '../lib/chartTheme.ts'
import { isDark } from '../composables/useTheme.ts'
import { STATIC_INTERACTION, useTimeZoom } from '../composables/useTimeZoom.ts'

// A chart.  Interaction is deliberately small: hover for values, and on time
// axes drag / pinch / Ctrl+wheel to move and zoom in time (useTimeZoom).
// The vertical axis is fixed; there is no box zoom, selection or mode bar.

function fixedY(layout) {
  const out = { ...layout }
  for (const key of Object.keys(out)) {
    if (/^yaxis\d*$/.test(key)) out[key] = { ...out[key], fixedrange: true }
  }
  out.xaxis = { ...out.xaxis, fixedrange: true }   // Plotly's own drag is off; see useTimeZoom
  return out
}

function mkLayout(extra = {}) {
  return fixedY(themeAxes({
    margin: { t: 10, r: 10, b: 60, l: 50 },
    ...baseLayout(11),
    legend: { orientation: 'h', y: -0.32, font: { size: 10 } },
    barmode: extra.barmode,
    ...STATIC_INTERACTION.layout,
    ...extra,
  }))
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
    const zoom = useTimeZoom()

    function buildLayout() {
      const width = el.value?.offsetWidth ?? 600
      // A fixed tick step (dtick) wins over the width-based tick count.
      const xaxis = props.layout.xaxis?.dtick
        ? { ...props.layout.xaxis }
        : { ...props.layout.xaxis, nticks: xNticks(width) }
      // New data must not throw the reader out of the stretch they zoomed into.
      const kept = zoom.keptRange()
      if (kept) { xaxis.range = kept; xaxis.autorange = false }
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
      const key = xAxisKey()
      if (initialized && key !== axisKey) {
        Plotly.purge(el.value)
        initialized = false
        zoom.clear()   // another time scale: start from the whole range
      }
      axisKey = key
      const layout = buildLayout()
      if (initialized) {
        Plotly.react(el.value, props.traces, layout, STATIC_INTERACTION.config)
      } else {
        Plotly.newPlot(el.value, props.traces, layout, STATIC_INTERACTION.config)
        initialized = true
      }
      zoom.drawn()
    }

    watch(() => [props.traces, props.layout], draw, { deep: true })
    // Text and grid colours come from the theme.
    watch(isDark, () => requestAnimationFrame(draw))

    onMounted(() => {
      zoom.attach(el.value)
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
      zoom.detach()
      if (el.value) Plotly.purge(el.value)
    })

    return () => h('div', { class: 'chart-wrap' }, [
      h('div', { ref: el, style: { height: props.height } }),
      zoom.zoomed.value
        ? h('button', { type: 'button', class: 'chart-reset', onClick: zoom.reset }, 'Show all')
        : null,
    ])
  },
})

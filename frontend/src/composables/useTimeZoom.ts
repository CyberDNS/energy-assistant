// Time-only interaction for charts: drag to move in time, pinch (or
// Ctrl/⌘ + wheel, which is what a trackpad pinch sends) to zoom in time,
// double-click or double-tap to see everything again.  The vertical axis
// never moves, and a vertical swipe on a phone still scrolls the page.
//
// Plotly's own drag modes (box zoom, select, …) are switched off for these
// charts; this layer changes the x-axis range itself.
import Plotly from 'plotly.js-basic-dist-min'
import { ref } from 'vue'

/* eslint-disable @typescript-eslint/no-explicit-any */
type Range = [number, number]

const MIN_SPAN_MS = 30 * 60_000

export function useTimeZoom() {
  /** True while the chart shows less than everything (for the reset button). */
  const zoomed = ref(false)
  let el: any = null
  let range: Range | null = null // what is shown while zoomed, in ms
  let frame = 0
  const pointers = new Map<number, number>() // pointer id → clientX
  let pinch: { span: number, range: Range } | null = null
  let lastTap = 0

  const axis = () => el?._fullLayout?.xaxis
  const isTime = () => axis()?.type === 'date'

  // What the chart shows when it shows everything, in ms: the range Plotly
  // chose (or the layout asked for) the last time it was drawn unzoomed.
  let whole: Range | null = null
  const full = () => whole

  function current(): Range | null {
    const a = axis()
    return a ? [a.r2l(a.range[0]), a.r2l(a.range[1])] : null
  }

  function apply(next: Range): void {
    const all = full()
    const a = axis()
    if (!all || !a) return
    let [lo, hi] = next
    const span = Math.min(Math.max(hi - lo, MIN_SPAN_MS), all[1] - all[0])
    const mid = (lo + hi) / 2
    lo = mid - span / 2
    hi = mid + span / 2
    if (lo < all[0]) { hi += all[0] - lo; lo = all[0] }
    if (hi > all[1]) { lo -= hi - all[1]; hi = all[1] }
    if (span >= all[1] - all[0] - 1) return reset()
    range = [lo, hi]
    zoomed.value = true
    cancelAnimationFrame(frame)
    frame = requestAnimationFrame(() => {
      if (el && range) Plotly.relayout(el, { 'xaxis.range': [a.l2r(range[0]), a.l2r(range[1])] })
    })
  }

  function reset(): void {
    range = null
    zoomed.value = false
    cancelAnimationFrame(frame)
    const a = axis()
    if (a && whole) Plotly.relayout(el, { 'xaxis.range': [a.l2r(whole[0]), a.l2r(whole[1])] })
  }

  /** Where in the plot area a pointer is, 0 (left) … 1 (right). */
  function fraction(clientX: number): number {
    const size = el._fullLayout._size
    const left = el.getBoundingClientRect().left + size.l
    return Math.min(1, Math.max(0, (clientX - left) / size.w))
  }

  function zoomAround(clientX: number, factor: number, from: Range | null = current()): void {
    if (!from) return
    const anchor = from[0] + (from[1] - from[0]) * fraction(clientX)
    apply([anchor - (anchor - from[0]) * factor, anchor + (from[1] - anchor) * factor])
  }

  function onWheel(e: WheelEvent): void {
    if (!isTime() || !(e.ctrlKey || e.metaKey)) return // a plain wheel scrolls the page
    e.preventDefault()
    // A trackpad pinch sends many small steps, a mouse wheel few large ones.
    zoomAround(e.clientX, Math.exp(Math.max(-30, Math.min(30, e.deltaY)) * 0.01))
  }

  function onPointerDown(e: PointerEvent): void {
    if (!isTime() || (e.pointerType === 'mouse' && e.button !== 0)) return
    pointers.set(e.pointerId, e.clientX)
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()]
      const now = current()
      if (now) pinch = { span: Math.abs(a - b) || 1, range: now }
    }
    if (pointers.size === 1) {
      // Second press shortly after the first, without a move in between.
      const t = Date.now()
      if (t - lastTap < 320) reset()
      lastTap = t
    }
  }

  function onPointerMove(e: PointerEvent): void {
    const before = pointers.get(e.pointerId)
    if (before == null || !el) return
    pointers.set(e.pointerId, e.clientX)
    if (pointers.size === 2 && pinch) {
      const [a, b] = [...pointers.values()]
      zoomAround((a + b) / 2, pinch.span / (Math.abs(a - b) || 1), pinch.range)
    } else if (pointers.size === 1) {
      const now = range ?? current()
      // Only a zoomed chart can be moved: at full view there is nowhere to go.
      if (!now || !zoomed.value) return
      const dx = e.clientX - before
      if (!dx) return
      lastTap = 0
      const shift = -dx / el._fullLayout._size.w * (now[1] - now[0])
      apply([now[0] + shift, now[1] + shift])
    }
  }

  function onPointerUp(e: PointerEvent): void {
    pointers.delete(e.pointerId)
    if (pointers.size < 2) pinch = null
  }

  function attach(element: HTMLElement): void {
    el = element
    // Horizontal moves are ours; vertical ones scroll the page.
    element.style.touchAction = 'pan-y'
    element.addEventListener('wheel', onWheel, { passive: false })
    element.addEventListener('pointerdown', onPointerDown)
    // On the window: Plotly covers the page with its own layer while a mouse
    // button is down, so the chart itself never sees the move or the release.
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
    window.addEventListener('pointercancel', onPointerUp)
  }

  function detach(): void {
    cancelAnimationFrame(frame)
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    window.removeEventListener('pointercancel', onPointerUp)
    el = null
  }

  /** The x range to keep when the chart is redrawn with new data, if zoomed. */
  function keptRange(): [string, string] | null {
    const a = axis()
    return range && a ? [a.l2r(range[0]), a.l2r(range[1])] : null
  }

  /** Call after every draw: an unzoomed chart defines what "everything" is. */
  function drawn(): void {
    if (!range) whole = current()
  }

  /** Forget the zoom without redrawing (the chart is rebuilt from scratch). */
  function clear(): void {
    range = null
    zoomed.value = false
  }

  return { zoomed, attach, detach, reset, keptRange, clear, drawn }
}

/** Layout and config that leave all interaction to useTimeZoom(). */
export const STATIC_INTERACTION = {
  layout: { dragmode: false as const },
  config: { displayModeBar: false, responsive: true, scrollZoom: false, doubleClick: false as const },
}

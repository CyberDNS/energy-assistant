// Colours of chart text and grid lines follow the theme (styles/main.css).
// Read at draw time; chart components redraw when `isDark` changes.
function token(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

export function plotColors() {
  return {
    text: token('--plot-text', '#5b6964'),
    grid: token('--plot-grid', '#e4e9e5'),
    zero: token('--plot-zero', '#c3ccc6'),
  }
}

type Axis = Record<string, unknown> | undefined

/** An axis with themed grid, zero line and tick colours (explicit settings win). */
export function themedAxis(axis: Axis): Record<string, unknown> {
  const c = plotColors()
  const minor = (axis?.minor as Record<string, unknown> | undefined)
  return {
    gridcolor: c.grid,
    zerolinecolor: c.zero,
    linecolor: c.grid,
    tickcolor: c.grid,
    automargin: true,
    ...axis,
    ...(minor ? { minor: { tickcolor: c.grid, ...minor } } : {}),
  }
}

/** Layout defaults shared by every chart. */
export function baseLayout(fontSize = 11): Record<string, unknown> {
  const c = plotColors()
  return {
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: { size: fontSize, color: c.text, family: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif' },
    hoverlabel: { font: { size: 12 } },
  }
}

/** Themes every x/y axis found in a layout (xaxis, yaxis, yaxis2, …). */
export function themeAxes(layout: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...layout }
  for (const key of new Set(['xaxis', 'yaxis', ...Object.keys(layout).filter(k => /^[xy]axis\d*$/.test(k))])) {
    out[key] = themedAxis(layout[key] as Axis)
  }
  return out
}

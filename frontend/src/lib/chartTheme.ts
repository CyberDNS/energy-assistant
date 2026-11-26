// Colours of chart text, grid lines and series follow the theme (styles/main.css).
// Read at draw time; chart components redraw when `isDark` changes, and a
// computed that builds traces from series() re-runs with it.
import { isDark } from '../composables/useTheme'

function token(name: string, fallback: string): string {
  void isDark.value   // a reactive dependency: the tokens change with the theme
  if (typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

/** One colour per thing shown (the --series-* tokens), the same in every chart. */
export function series() {
  return {
    solar: token('--series-solar', '#b86e00'),
    battery: token('--series-battery', '#2a78d6'),
    import: token('--series-import', '#e34948'),
    export: token('--series-export', '#e87ba4'),
    home: token('--series-home', '#6250d6'),
    car: token('--series-car', '#1baf7a'),
    saving: token('--series-saving', '#008300'),
    neutral: token('--series-neutral', '#8b93a7'),
  }
}

/** *hex* (#rrggbb) at *alpha*, for fills under a line. */
export function withAlpha(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex)
  if (!m) return hex
  const n = Number.parseInt(m[1], 16)
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${alpha})`
}

export function plotColors() {
  return {
    text: token('--plot-text', '#5a6378'),
    grid: token('--plot-grid', '#e3e6ee'),
    zero: token('--plot-zero', '#c3c8d6'),
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

// Colours of series that are not one of the things in series() (several
// batteries, several cars …), in fixed order; validated for colour-vision
// deficiency on the light and on the dark surface.
const COLORWAY_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948']
const COLORWAY_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#0ca30c', '#9085e9', '#e66767']

/** Layout defaults shared by every chart. */
export function baseLayout(fontSize = 11): Record<string, unknown> {
  const c = plotColors()
  return {
    colorway: isDark.value ? COLORWAY_DARK : COLORWAY_LIGHT,
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

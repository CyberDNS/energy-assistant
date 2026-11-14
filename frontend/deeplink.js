// Deep links and embed mode.
//
// Hash routes are the canonical form:
//   #/plan                    Plan tab
//   #/plan/<vehicle>          one vehicle only (asset_id, case-insensitive)
//   #/plan?vehicle=<vehicle>  same
//   …?embed=1                 hide header, KPI tiles and tab bar (iframe use)
//
// Hosts that drop the URL fragment (e.g. the Home Assistant ingress panel)
// can pass the same thing as a query string on the page URL instead:
//   ?view=plan   ?vehicle=<vehicle>   ?embed=1
//
// When neither is given, the last-used tab is restored.

const LAST_TAB_KEY = 'energy-assistant.lastTab'
const TABS = ['/', '/plan']

const search = new URLSearchParams(window.location.search)

export function isOn(v) {
  return ['1', 'true', 'yes', 'on'].includes(String(v ?? '').toLowerCase())
}

// True when ?embed=1 was given on the page URL (before the '#').
export const embedFromSearch = isOn(search.get('embed'))

function lastTab() {
  try {
    const t = localStorage.getItem(LAST_TAB_KEY)
    return TABS.includes(t) ? t : null
  } catch {
    return null
  }
}

export function rememberTab(path) {
  if (!TABS.includes(path)) return
  try { localStorage.setItem(LAST_TAB_KEY, path) } catch { /* storage blocked */ }
}

// Call before the router is created: when the URL carries no hash route,
// derive one from the query string or the remembered tab.
export function applyInitialRoute() {
  if (window.location.hash.length > 1) return
  const vehicle = search.get('vehicle')
  const view = search.get('view')
  let target = null
  if (vehicle) target = '/plan/' + encodeURIComponent(vehicle)
  else if (view === 'plan') target = '/plan'
  else if (view === 'live') target = '/'
  else if (!embedFromSearch) target = lastTab()
  if (target && target !== '/') {
    window.history.replaceState(
      window.history.state, '',
      window.location.pathname + window.location.search + '#' + target,
    )
  }
}

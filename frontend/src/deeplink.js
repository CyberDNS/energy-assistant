// Deep links and embed mode.
//
// Hash routes are the canonical form:
//   #/plan                    Plan
//   #/cars/<vehicle>          one car (vehicle id or label, case-insensitive)
//   #/devices/<device>        one device
//   #/savings  #/learning  #/batteries  #/loads  #/devices  #/settings/<page>
//   …?embed=1                 content only: no sidebar, header or status bar (iframe use)
//
// Hosts that drop the URL fragment (e.g. the Home Assistant ingress panel)
// can pass the same thing as a query string on the page URL instead:
//   ?view=plan   ?view=savings   ?vehicle=<vehicle>   ?embed=1
//
// Links from before the sidebar keep working: #/plan/<vehicle> and
// #/plan?vehicle=<vehicle> open the car, #/config/<page> the settings page
// (see the redirects in router/index.ts).
//
// When neither is given, the page used last is restored.

const LAST_KEY = 'nornwatt.lastTab'
const VIEWS = {
  live: '/', home: '/', plan: '/plan', cars: '/cars', batteries: '/batteries', loads: '/loads',
  devices: '/devices', savings: '/savings', history: '/devices', learning: '/learning', settings: '/settings',
  config: '/settings',
}
const PAGES = new Set(Object.values(VIEWS))

const search = new URLSearchParams(window.location.search)

export function isOn(v) {
  return ['1', 'true', 'yes', 'on'].includes(String(v ?? '').toLowerCase())
}

// True when ?embed=1 was given on the page URL (before the '#').
export const embedFromSearch = isOn(search.get('embed'))

// The top-level page of a route path: '/settings/devices' → '/settings'.
function pageOf(path) {
  const top = '/' + (String(path).split('/')[1] ?? '')
  return PAGES.has(top) ? top : null
}

function lastPage() {
  try {
    const stored = localStorage.getItem(LAST_KEY)
    // Before the sidebar the Config tab was remembered as '/config'.
    return pageOf(stored === '/config' ? '/settings' : stored)
  } catch {
    return null
  }
}

export function rememberPage(path) {
  const page = pageOf(path)
  if (!page) return
  try { localStorage.setItem(LAST_KEY, page) } catch { /* storage blocked */ }
}

// The route asked for by the page URL's query string, or null.
export function routeFromSearch() {
  const vehicle = search.get('vehicle')
  if (vehicle) return '/cars/' + encodeURIComponent(vehicle)
  const view = search.get('view')
  return view ? (VIEWS[view.toLowerCase()] ?? null) : null
}

// Call before the router is created: when the URL carries no hash route,
// derive one from the query string or the page used last.
export function applyInitialRoute() {
  if (window.location.hash.length > 1) return
  const target = routeFromSearch() ?? (embedFromSearch ? null : lastPage())
  if (target && target !== '/') {
    window.history.replaceState(
      window.history.state, '',
      window.location.pathname + window.location.search + '#' + target,
    )
  }
}

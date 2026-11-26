// Embedding in Home Assistant.
//
// Opened from the Home Assistant sidebar, the app runs in an iframe of HA's
// app panel (`ha-panel-app`, HA 2026.2 and later).  That panel speaks a small
// postMessage protocol:
//
//   app → HA   home-assistant/subscribe-properties { kioskMode, handleSafeArea }
//              home-assistant/unsubscribe-properties
//              home-assistant/toggle-menu
//              home-assistant/navigate { path, options }
//   HA → app   home-assistant/properties { narrow, route, safeAreaInsets }
//
// With kiosk mode HA hides its own sidebar and header, so our sidebar is the
// only one; `toggleHAMenu()` opens HA's sidebar as an overlay (the "Home
// Assistant" button).  Older HA versions and other hosts ignore the messages:
// nothing is ever received and `inHomeAssistant` stays false.
import { reactive, readonly } from 'vue'

export interface HARoute {
  prefix: string
  path: string
}

interface HAState {
  /** True once Home Assistant has answered: we are inside its app panel. */
  inHomeAssistant: boolean
  subscribed: boolean
  kioskMode: boolean
  narrow: boolean
  route: HARoute | null
}

const KIOSK_KEY = 'nornwatt.haKiosk'
const INSET_EDGES = ['top', 'right', 'bottom', 'left'] as const

const state = reactive<HAState>({
  inHomeAssistant: false,
  subscribed: false,
  kioskMode: false,
  narrow: false,
  route: null,
})

let insets: Partial<Record<(typeof INSET_EDGES)[number], string>> | null = null
const routeListeners = new Set<(route: HARoute) => void>()

/** True when the page runs in a frame that could be Home Assistant's. */
export function isFramed(): boolean {
  try {
    return window.parent !== window
  } catch {
    return true
  }
}

function post(message: Record<string, unknown>): void {
  window.parent.postMessage(message, '*')
}

// While Home Assistant is narrow and not in kiosk mode it draws a header above
// the frame that already covers the top inset.
function applyInsets(): void {
  const headerShown = state.narrow && !state.kioskMode
  for (const edge of INSET_EDGES) {
    const value = state.subscribed && insets && !(edge === 'top' && headerShown) ? insets[edge] : ''
    const property = `--device-inset-${edge}`
    if (value) document.documentElement.style.setProperty(property, value)
    else document.documentElement.style.removeProperty(property)
  }
}

function onMessage(event: MessageEvent): void {
  if (event.source !== window.parent || event.data?.type !== 'home-assistant/properties') return
  state.inHomeAssistant = true
  state.narrow = !!event.data.narrow
  const route: HARoute | null = event.data.route ?? null
  if (event.data.safeAreaInsets) insets = event.data.safeAreaInsets
  applyInsets()
  const changed = route && (route.path !== state.route?.path || route.prefix !== state.route?.prefix)
  state.route = route
  if (route && changed) for (const listener of routeListeners) listener(route)
}

/** Whether the app takes over Home Assistant's whole screen (default: yes). */
export function kioskModePreference(): boolean {
  try {
    return localStorage.getItem(KIOSK_KEY) !== 'false'
  } catch {
    return true
  }
}

function subscribe(kioskMode: boolean): void {
  post({ type: 'home-assistant/subscribe-properties', kioskMode, handleSafeArea: true })
  state.subscribed = true
  state.kioskMode = kioskMode
}

function unsubscribe(): void {
  post({ type: 'home-assistant/unsubscribe-properties' })
  state.subscribed = false
  state.kioskMode = false
  applyInsets()
}

/** Start talking to Home Assistant.  A no-op outside a frame. */
export function connectHomeAssistant(): void {
  if (!isFramed() || state.subscribed) return
  window.addEventListener('message', onMessage)
  subscribe(kioskModePreference())
  // HA leaves kiosk mode by itself when the panel goes away; when only this
  // frame is reloaded it would otherwise stay on with nobody subscribed.
  window.addEventListener('pagehide', () => {
    if (state.subscribed) post({ type: 'home-assistant/unsubscribe-properties' })
  })
}

/** Store the kiosk mode choice of this device and apply it right away. */
export function setKioskModePreference(enabled: boolean): void {
  try {
    localStorage.setItem(KIOSK_KEY, String(enabled))
  } catch {
    /* storage blocked: applies until the next reload */
  }
  if (!state.subscribed || enabled === state.kioskMode) return
  // A subscription can only switch kiosk mode on; leaving it takes an unsubscribe.
  unsubscribe()
  subscribe(enabled)
}

/** Open or close Home Assistant's sidebar (an overlay while in kiosk mode). */
export function toggleHAMenu(): void {
  post({ type: 'home-assistant/toggle-menu' })
}

/** Navigate Home Assistant itself, e.g. to "/config" or a dashboard. */
export function navigateInHA(path: string, options: { replace?: boolean } = {}): void {
  post({ type: 'home-assistant/navigate', path, options })
}

/** Called with Home Assistant's route for the app whenever it changes. */
export function onHARoute(listener: (route: HARoute) => void): () => void {
  routeListeners.add(listener)
  return () => routeListeners.delete(listener)
}

export const haState = readonly(state)

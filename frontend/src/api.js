// The app root, so API calls work both directly (http://host:8088/) and behind
// Home Assistant ingress (https://ha-host/api/hassio_ingress/<token>/): the page
// itself is served at the root, so its own URL without file, query and hash is it.
const _apiBase = new URL('.', document.baseURI).href.replace(/\/$/, '')

async function call(url, options) {
  const res = await fetch(_apiBase + url, options)
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`)
  return res.json()
}

export const fetchStatus   = ()           => call('/api/status')
export const fetchVersion  = ()           => call('/api/version')
export const streamUrl     = _apiBase + '/api/stream'
export const fetchPlan     = ()           => call('/api/plan')
export const fetchForecast = ()           => call('/api/forecast')
// ids: device ids (default: batteries, meters and PV).  The server picks 5-minute
// or hourly buckets by the range.
export const fetchHistory  = (hours, ids = []) =>
  call(`/api/history?hours=${hours}` + (ids.length ? `&device_ids=${encodeURIComponent(ids.join(','))}` : ''))
export const fetchLedger   = ()           => call('/api/ledger')
export const fetchVehicles     = ()           => call('/api/vehicles')
export const fetchControllable = ()           => call('/api/controllable')
export const fetchConfig   = ()           => call('/api/config')
// { summary: true }: status and model parameters only (no chart data);
// { deviceId }: only that device's models.
/** @param {{ summary?: boolean, deviceId?: string | null }} [opts] */
export const fetchLearned  = ({ summary = false, deviceId = null } = {}) => {
  const q = new URLSearchParams()
  if (summary) q.set('summary', 'true')
  if (deviceId != null) q.set('device_id', deviceId)
  return call(q.size ? `/api/learned?${q}` : '/api/learned')
}
export const fetchSavings  = (hours)      => call(`/api/savings?hours=${hours}`)
export const refitLearned  = (id)         => call(`/api/learned/${encodeURIComponent(id)}/refit`, { method: 'POST' })

const _json = (method, body) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const fetchEvPlan = (id) => call(`/api/vehicles/${encodeURIComponent(id)}/plan`)
export const saveEvWeeklyPlan = (id, weekly) =>
  call(`/api/vehicles/${encodeURIComponent(id)}/plan`, _json('PUT', { weekly }))
export const setEvDayOverride = (id, date, override) =>
  call(`/api/vehicles/${encodeURIComponent(id)}/plan/${date}`, _json('PUT', override))
export const clearEvDayOverride = (id, date) =>
  call(`/api/vehicles/${encodeURIComponent(id)}/plan/${date}`, { method: 'DELETE' })
export const startForceCharge = (id, socPct) =>
  call(`/api/vehicles/${encodeURIComponent(id)}/force_charge?target_soc_pct=${socPct}`, { method: 'POST' })
export const stopForceCharge = (id) =>
  call(`/api/vehicles/${encodeURIComponent(id)}/force_charge`, { method: 'DELETE' })

export const disableChargepoint  = (id) => call(`/api/vehicles/${encodeURIComponent(id)}/disable`, { method: 'POST' })
export const enableChargepoint   = (id) => call(`/api/vehicles/${encodeURIComponent(id)}/disable`, { method: 'DELETE' })
export const setLedgerBasis      = (deviceId, basis) =>
  call(`/api/ledger/set_basis?device_id=${encodeURIComponent(deviceId)}&cost_basis_eur_per_kwh=${basis}`, { method: 'POST' })

export const triggerPlanRefresh  = () => call('/api/plan/refresh', { method: 'POST' })

export const fetchChargers = () => call('/api/chargers')
// Put a car (or "guest") on a charger; "none" drops its selection.
export const selectEvCharger = (vehicleId, chargerId) =>
  call(`/api/vehicles/${encodeURIComponent(vehicleId)}/charger?charger_id=${encodeURIComponent(chargerId)}`,
       { method: 'PUT' })
// How the guest car charges: "pv_only" or "instant".
export const setGuestMode = (mode) =>
  call(`/api/guest/mode?mode=${encodeURIComponent(mode)}`, { method: 'PUT' })

// ── Configuration editor (Devices tab) ──────────────────────────────────
// These calls report validation problems as a list, so they resolve to
// { ok, errors, ...body } instead of throwing on 4xx.
async function configCall(url, options) {
  let res
  try {
    res = await fetch(_apiBase + url, options)
  } catch (e) {
    return { ok: false, errors: [`Server not reachable: ${e.message}`] }
  }
  let body = null
  try { body = await res.json() } catch { /* empty or non-JSON body */ }
  if (res.ok) return { ok: true, errors: [], ...(body ?? {}) }
  const detail = body?.detail
  const errors = detail?.errors
    ?? (typeof detail === 'string' ? [detail] : [`${res.status} ${res.statusText}`])
  return { ok: false, status: res.status, errors }
}

export const fetchConfigSchema   = () => call('/api/config/schema')
export const fetchConfigFile     = () => call('/api/config/file')
export const fetchConfigEntities = () => call('/api/config/entities')
// kind: 'devices' | 'tariffs' | 'forecasts' | 'section'
export const validateConfigChange = (kind, id, value, create) =>
  configCall('/api/config/validate', _json('POST', { kind, id, value, create }))
export const saveConfigChange = (kind, id, value, revision, create) =>
  configCall(`/api/config/${kind === 'section' ? 'sections' : kind}/${encodeURIComponent(id)}`,
             _json('PUT', { value, revision, create }))
export const deleteConfigEntry = (kind, id, revision) =>
  configCall(`/api/config/${kind}/${encodeURIComponent(id)}?revision=${encodeURIComponent(revision)}`,
             { method: 'DELETE' })
export const setConfigSecret = (name, value) =>
  configCall(`/api/config/secrets/${encodeURIComponent(name)}`, _json('PUT', { value }))
export const deleteConfigSecret = (name) =>
  configCall(`/api/config/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' })
export const reloadConfig = () => configCall('/api/config/reload', { method: 'POST' })
// Observe mode (#134): 'observe' | 'active', written to config.yaml and applied at once.
export const setDeviceControl = (id, control) =>
  configCall(`/api/config/devices/${encodeURIComponent(id)}/control`, _json('PUT', { control }))
// config.yaml as text (the editor of last resort, also when it is not valid YAML).
export const fetchConfigText = () => configCall('/api/config/text')
export const saveConfigText = (text, revision) =>
  configCall('/api/config/text', _json('PUT', { text, revision }))
// Small and cheap: whether config.yaml on disk differs from what is running.
export const fetchConfigState = () => call('/api/config/state')
// Savings slots since `start` (a Date), with their totals.
export const fetchSavingsSince = (start) => call(`/api/savings/slots?start=${encodeURIComponent(start.toISOString())}`)

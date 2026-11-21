// Resolve the addon root relative to this file's URL so API calls work
// both in direct mode (http://host:8088/) and behind HASS ingress
// (https://ha-host/api/hassio_ingress/<token>/).
// api.js lives at <root>/ui/api.js → one level up is <root>.
const _apiBase = new URL('..', import.meta.url).href.replace(/\/$/, '')

async function call(url, options) {
  const res = await fetch(_apiBase + url, options)
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`)
  return res.json()
}

export const fetchStatus   = ()           => call('/api/status')
export const streamUrl     = _apiBase + '/api/stream'
export const fetchPlan     = ()           => call('/api/plan')
export const fetchForecast = ()           => call('/api/forecast')
export const fetchHistory  = (hours)      => call(`/api/history?hours=${hours}`)
export const fetchLedger   = ()           => call('/api/ledger')
export const fetchEv           = ()           => call('/api/ev')
export const fetchControllable = ()           => call('/api/controllable')
export const fetchConfig   = ()           => call('/api/config')
export const fetchLearned  = ()           => call('/api/learned')
export const fetchSavings  = (hours)      => call(`/api/savings?hours=${hours}`)
export const refitLearned  = (id)         => call(`/api/learned/${encodeURIComponent(id)}/refit`, { method: 'POST' })

const _json = (method, body) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const fetchEvPlan = (id) => call(`/api/ev/${encodeURIComponent(id)}/plan`)
export const saveEvWeeklyPlan = (id, weekly) =>
  call(`/api/ev/${encodeURIComponent(id)}/plan`, _json('PUT', { weekly }))
export const setEvDayOverride = (id, date, override) =>
  call(`/api/ev/${encodeURIComponent(id)}/plan/${date}`, _json('PUT', override))
export const clearEvDayOverride = (id, date) =>
  call(`/api/ev/${encodeURIComponent(id)}/plan/${date}`, { method: 'DELETE' })
export const startForceCharge = (id, socPct) =>
  call(`/api/ev/${encodeURIComponent(id)}/force_charge?target_soc_pct=${socPct}`, { method: 'POST' })
export const stopForceCharge = (id) =>
  call(`/api/ev/${encodeURIComponent(id)}/force_charge`, { method: 'DELETE' })

export const disableChargepoint  = (id) => call(`/api/ev/${encodeURIComponent(id)}/disable`, { method: 'POST' })
export const enableChargepoint   = (id) => call(`/api/ev/${encodeURIComponent(id)}/disable`, { method: 'DELETE' })
export const setLedgerBasis      = (deviceId, basis) =>
  call(`/api/ledger/set_basis?device_id=${encodeURIComponent(deviceId)}&cost_basis_eur_per_kwh=${basis}`, { method: 'POST' })

export const triggerPlanRefresh  = () => call('/api/plan/refresh', { method: 'POST' })

export const fetchChargers = () => call('/api/chargers')
// Put a car (or "guest") on a charger; "none" drops its selection.
export const selectEvCharger = (vehicleId, chargerId) =>
  call(`/api/ev/${encodeURIComponent(vehicleId)}/charger?charger_id=${encodeURIComponent(chargerId)}`,
       { method: 'PUT' })
// How the guest car charges: "pv_only" or "instant".
export const setGuestMode = (mode) =>
  call(`/api/ev/guest/mode?mode=${encodeURIComponent(mode)}`, { method: 'PUT' })

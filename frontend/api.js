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
export const fetchPlan     = ()           => call('/api/plan')
export const fetchForecast = ()           => call('/api/forecast')
export const fetchHistory  = (hours)      => call(`/api/history?hours=${hours}`)
export const fetchLedger   = ()           => call('/api/ledger')
export const fetchEv       = ()           => call('/api/ev')
export const fetchConfig   = ()           => call('/api/config')

export function setEvTarget(assetId, socPct, targetBy) {
  const by = targetBy instanceof Date ? targetBy.toISOString() : targetBy
  const url = `/api/ev/${encodeURIComponent(assetId)}/set_target`
    + `?target_soc_pct=${socPct}&target_by=${encodeURIComponent(by)}`
  return call(url, { method: 'POST' })
}

export const clearEvTarget       = (id) => call(`/api/ev/${encodeURIComponent(id)}/target`,  { method: 'DELETE' })
export const disableChargepoint  = (id) => call(`/api/ev/${encodeURIComponent(id)}/disable`, { method: 'POST' })
export const enableChargepoint   = (id) => call(`/api/ev/${encodeURIComponent(id)}/disable`, { method: 'DELETE' })
export const setLedgerBasis      = (deviceId, basis) =>
  call(`/api/ledger/set_basis?device_id=${encodeURIComponent(deviceId)}&cost_basis_eur_per_kwh=${basis}`, { method: 'POST' })

export const triggerPlanRefresh  = () => call('/api/plan/refresh', { method: 'POST' })

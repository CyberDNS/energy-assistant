// Number formatting shared by the shell and the views.
const DASH = '—'

export function fmt(v: number | null | undefined, decimals = 0): string {
  return v == null ? DASH : Number(v).toFixed(decimals)
}

/** 850 → "850 W", 2310 → "2.31 kW". */
export function fmtPower(w: number | null | undefined): string {
  if (w == null) return DASH
  const abs = Math.abs(w)
  return abs >= 1000 ? `${(w / 1000).toFixed(2)} kW` : `${Math.round(w)} W`
}

/** 0.2840 €/kWh → "28.40 ct". */
export function fmtPrice(eurPerKwh: number | null | undefined): string {
  return eurPerKwh == null ? DASH : `${(Number(eurPerKwh) * 100).toFixed(2)} ct`
}

/** Seconds since an ISO timestamp as "12s ago" / "3 min ago". */
export function fmtAgo(iso: string | null | undefined, now = Date.now()): string | null {
  if (!iso) return null
  const sec = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000))
  return sec < 90 ? `${sec}s ago` : `${Math.round(sec / 60)} min ago`
}

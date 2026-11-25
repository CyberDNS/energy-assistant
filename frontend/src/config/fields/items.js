// Naming of list items in the config UI: what one item is called ("period"
// → "+ Add period", "Remove period") and the title of an item's card.

// What one item of the list / map `spec` is called: spec.item_label, else the
// field label in the singular ("Roof planes" → "roof plane"), else `fallback`.
export function itemLabelOf(spec, fallback = 'item') {
  if (spec?.item_label) return spec.item_label
  const label = (spec?.label ?? '').trim()
  if (!label || /[(\d]/.test(label)) return fallback
  const lower = label.charAt(0).toLowerCase() + label.slice(1)
  return lower.endsWith('s') && !lower.endsWith('ss') ? lower.slice(0, -1) : lower
}

// "period" → "Period".
export const capitalize = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s)

// The title of item `value` (number `index`, 0-based) of a list of records
// whose item spec is `itemSpec`: its summary template filled in, else the
// value of a `name` field; empty when nothing is filled in yet.
export function summaryOf(itemSpec, value) {
  const v = value && typeof value === 'object' ? value : {}
  const template = itemSpec?.summary
  if (template) {
    const keys = [...template.matchAll(/\{(\w+)\}/g)].map(m => m[1])
    if (keys.some(k => v[k] == null || v[k] === '')) return v.name != null && v.name !== '' ? String(v.name) : ''
    return template.replace(/\{(\w+)\}/g, (_, k) => formatValue(itemSpec.fields?.[k], v[k]))
  }
  return v.name != null && v.name !== '' ? String(v.name) : ''
}

// Whether a record has fields that hold other lists or mappings (edited row
// by row, not by one control such as the weekday toggles): such records get a
// card each instead of a table row (a table inside a table is unreadable).
export function hasNestedFields(fields) {
  return Object.values(fields ?? {}).some(s => !s.widget
    && ((s.kind === 'list' && s.items) || (s.kind === 'dict' && (s.fields || s.values))))
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// A value as text for a title: "11-01" of a month_day field → "1 Nov".
export function formatValue(spec, value) {
  if (spec?.widget === 'month_day') {
    const m = /^(\d{1,2})-(\d{1,2})$/.exec(String(value))
    if (m && MONTHS[Number(m[1]) - 1]) return `${Number(m[2])} ${MONTHS[Number(m[1]) - 1]}`
  }
  return String(value)
}

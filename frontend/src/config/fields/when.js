// When a field applies: `spec.when` conditions on sibling fields, and the
// selectors (UI-only switches) they may name.  Mirrors ParamSpec.applies and
// ParamSpec.selected (core/device_model.py).
import { isEmpty } from '../useConfig.js'

export const holds = (v) => !isEmpty(v) || v === false || v === 0
const names = (wanted, choice) => wanted === choice || (Array.isArray(wanted) && wanted.includes(choice))

// A selector's value: the first choice whose fields hold a value, else
// `picked`, else the default.  Mirrors ParamSpec.selected (core/device_model.py).
export function selected(key, values, specs, picked) {
  const spec = specs[key]
  for (const choice of spec.choices ?? []) {
    if (Object.entries(specs).some(([name, s]) => s.when && key in s.when && names(s.when[key], choice)
        && holds(values[name]))) return choice
  }
  return picked ?? spec.default
}

// Mirrors ParamSpec.applies (core/device_model.py).
export function applies(spec, values, specs) {
  for (const [key, wanted] of Object.entries(spec.when ?? {})) {
    let value = values[key]
    if (specs[key]?.selector && !holds(value)) value = selected(key, values, specs)
    else if (isEmpty(value) && value !== false && value !== 0) value = specs[key]?.default
    const empty = isEmpty(value) && value !== false && value !== 0
    const ok = wanted === null ? empty
      : wanted === '*' ? !empty
      : Array.isArray(wanted) ? wanted.includes(value) : value === wanted
    if (!ok) return false
  }
  return true
}

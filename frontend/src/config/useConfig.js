import { ref, computed, provide, inject, watch, onUnmounted } from 'vue'
import {
  fetchConfigSchema, fetchConfigFile, fetchConfigEntities,
  validateConfigChange, saveConfigChange, deleteConfigEntry,
} from '../api.js'

const KEY = Symbol('config')

export const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)))

// True for values that are left out of config.yaml.
export function isEmpty(v) {
  if (v == null || v === '') return true
  if (Array.isArray(v)) return v.length === 0
  if (typeof v === 'object') return Object.keys(v).length === 0
  return false
}

// "409: {"detail":{"errors":[…]}}" → the problems themselves.
function readableError(e) {
  const text = String(e).replace(/^Error: /, '')
  const m = /^\d+: (.*)$/s.exec(text)
  try {
    const errors = JSON.parse(m?.[1] ?? '')?.detail?.errors
    if (Array.isArray(errors) && errors.length) return errors.join(' · ')
  } catch { /* not JSON */ }
  return text
}

// Shared state of the configuration UI: the schemas, the content of
// config.yaml and what the pickers offer (devices, tariffs, entities, secrets).
// Created once by ConfigView; every editor component reads it with useConfig().
export function provideConfig() {
  const schema = ref(null)
  const file = ref(null)
  const entities = ref([])
  const error = ref(null)

  async function load() {
    try {
      if (!schema.value) schema.value = await fetchConfigSchema()
      file.value = await fetchConfigFile()
      error.value = null
    } catch (e) {
      error.value = readableError(e)
    }
  }
  async function loadEntities() {
    try { entities.value = await fetchConfigEntities() } catch { /* pickers work without */ }
  }

  const devices = computed(() => [...(file.value?.devices ?? []), ...(file.value?.imported ?? [])]
    .map(d => ({ id: d.id, label: d.label || d.id, category: d.category })))
  const tariffs = computed(() => (file.value?.tariffs ?? []).map(t => t.id))
  // The tariff of everything that names none: the one marked default, else the first.
  const defaultTariff = computed(() => {
    const list = file.value?.tariffs ?? []
    return (list.find(t => t.settings?.default) ?? list[0])?.id ?? null
  })
  const pluginTitle = (kind, type) => plugins(kind).find(p => p.type === type)?.title ?? type
  const categoryTitle = (category) => schema.value?.categories.find(c => c.category === category)?.title ?? category
  const capabilityTitle = (cap) => schema.value?.capabilities.find(c => c.capability === cap)?.title ?? cap
  const secrets = computed(() => file.value?.secrets ?? [])

  function plugins(kind, scope = '') {
    const list = kind === 'tariff' ? schema.value.tariff_plugins
      : kind === 'forecast' ? schema.value.forecast_plugins : schema.value.plugins
    return scope ? list.filter(p => !p.scopes?.length || p.scopes.includes(scope)) : list
  }

  const ctx = { schema, file, entities, error, devices, tariffs, defaultTariff, secrets, plugins,
                pluginTitle, categoryTitle, capabilityTitle, load, loadEntities }
  provide(KEY, ctx)
  return ctx
}

export function useConfig() {
  return inject(KEY)
}

// Validation and saving of one change (a device, a tariff, a forecast or a
// settings section).  `value` is a getter for what would be saved.  The server
// checks it against the whole file a moment after every edit; save() writes
// config.yaml and applies it.
export function useConfigForm({ kind, id, value, create = false, ready = () => true, watchSource }) {
  const cfg = useConfig()
  const errors = ref([])
  const checked = ref(false)
  const busy = ref(false)
  let timer = null
  let seq = 0

  async function check() {
    if (!ready()) { errors.value = []; checked.value = false; return }
    const mine = ++seq
    const res = await validateConfigChange(kind, id(), clone(value()), create)
    if (mine !== seq) return   // a newer check is on its way
    errors.value = res.ok ? [] : res.errors
    checked.value = true
  }
  const stop = watch(watchSource ?? value, () => {
    checked.value = false
    clearTimeout(timer)
    timer = setTimeout(check, 500)
  }, { deep: true })
  onUnmounted(() => { stop(); clearTimeout(timer) })

  async function finish(res) {
    busy.value = false
    if (!res.ok) { errors.value = res.errors; checked.value = true; return null }
    await cfg.load()
    return res
  }
  async function save() {
    busy.value = true
    clearTimeout(timer)
    return finish(await saveConfigChange(kind, id(), clone(value()), cfg.file.value.revision, create))
  }
  async function remove() {
    busy.value = true
    return finish(await deleteConfigEntry(kind, id(), cfg.file.value.revision))
  }
  const canSave = computed(() => !busy.value && ready() && !(checked.value && errors.value.length))
  const valid = computed(() => ready() && checked.value && !errors.value.length)

  return { errors, checked, busy, check, save, remove, canSave, valid }
}

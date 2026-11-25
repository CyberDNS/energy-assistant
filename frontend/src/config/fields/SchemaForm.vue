<script>
import { defineComponent, computed, inject, provide } from 'vue'
import { isEmpty } from '../useConfig.js'
import { FORM_SCOPE } from './scope.js'

// A grid of fields for a mapping described by `specs` ({ key: spec }).
// v-model is the mapping itself; empty values are dropped, and an entirely
// empty mapping is emitted as undefined.  Keys the specs do not know are kept
// and, with `extras`, shown in a raw editor ("other settings").
//
// Which fields show:
//   spec.when      only while the named sibling fields have the given values
//                  (`context` adds values from outside this mapping)
//   spec.advanced  under "More settings"
//   spec.legacy    only where the file already uses it
// A field that holds a value is always shown, so it can be seen and cleared;
// changing a choice drops the values of the fields that no longer apply.

// Mirrors ParamSpec.applies (core/device_model.py).
export function applies(spec, values, specs) {
  for (const [key, wanted] of Object.entries(spec.when ?? {})) {
    let value = values[key]
    if (isEmpty(value) && value !== false && value !== 0) value = specs[key]?.default
    const empty = isEmpty(value) && value !== false && value !== 0
    const ok = wanted === null ? empty
      : wanted === '*' ? !empty
      : Array.isArray(wanted) ? wanted.includes(value) : value === wanted
    if (!ok) return false
  }
  return true
}
export default defineComponent({
  name: 'SchemaForm',
  props: {
    specs:      { type: Object, required: true },
    modelValue: { default: undefined },
    selfId:     { type: String, default: '' },
    extras:     { type: Boolean, default: false },
    extrasLabel:{ type: String, default: 'other settings' },
    keyPrefix:  { type: String, default: '' },
    context:    { type: Object, default: () => ({}) },
    showKeys:   { type: Boolean, default: true },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const values = computed(() => (props.modelValue && typeof props.modelValue === 'object' ? props.modelValue : {}))
    // Fields inside can look up values of this form and the forms around it.
    const parentScope = inject(FORM_SCOPE, null)
    provide(FORM_SCOPE, {
      lookup: (key, accept = () => true) => (key in values.value && accept(values.value[key]) ? values.value[key]
        : key in props.context && accept(props.context[key]) ? props.context[key] : parentScope?.lookup(key, accept)),
    })
    const extra = computed(() => Object.fromEntries(
      Object.entries(values.value).filter(([k]) => !(k in props.specs))))
    function emitValues(next) {
      const cleaned = Object.fromEntries(Object.entries(next).filter(([, v]) => !isEmpty(v) || v === false || v === 0))
      emit('update:modelValue', Object.keys(cleaned).length ? cleaned : undefined)
    }
    function set(key, v) {
      const next = { ...values.value, [key]: v }
      // Settings that only applied to the previous choice go with it.
      for (const [name, spec] of Object.entries(props.specs)) {
        if (spec.when && key in spec.when && !applies(spec, { ...props.context, ...next }, props.specs)) delete next[name]
      }
      emitValues(next)
    }
    function setExtra(obj) {
      const known = Object.fromEntries(Object.entries(values.value).filter(([k]) => k in props.specs))
      emitValues({ ...known, ...(obj ?? {}) })
    }
    const isSet = (name) => { const v = values.value[name]; return !isEmpty(v) || v === false || v === 0 }
    const visible = computed(() => Object.entries(props.specs).filter(([name, spec]) =>
      isSet(name) || (!spec.legacy && applies(spec, { ...props.context, ...values.value }, props.specs))))
    const main = computed(() => visible.value.filter(([, spec]) => !spec.advanced))
    const advanced = computed(() => visible.value.filter(([, spec]) => spec.advanced))
    const advancedSet = computed(() => advanced.value.filter(([name]) => isSet(name)).length)
    return { values, extra, set, setExtra, main, advanced, advancedSet }
  },
})
</script>

<template>
  <div class="cfg-schema-form">
    <div v-if="main.length" class="cfg-grid">
      <SchemaField v-for="[name, spec] in main" :key="keyPrefix + name" :name="name" :spec="spec" :show-key="showKeys"
                   :model-value="values[name]" :self-id="selfId" @update:model-value="set(name, $event)" />
    </div>
    <details v-if="advanced.length" class="cfg-advanced" :open="advancedSet > 0">
      <summary>More settings<span v-if="advancedSet" class="cfg-count">{{ advancedSet }} set</span></summary>
      <div class="cfg-grid">
        <SchemaField v-for="[name, spec] in advanced" :key="keyPrefix + name" :name="name" :spec="spec" :show-key="showKeys"
                     :model-value="values[name]" :self-id="selfId" @update:model-value="set(name, $event)" />
      </div>
    </details>
    <div v-if="extras && Object.keys(extra).length" class="cfg-field">
      <div class="cfg-label" role="group" :aria-label="extrasLabel">
        <span class="cfg-name">{{ extrasLabel }}</span>
        <span class="cfg-hint">Keys without a form field, as JSON.</span>
        <JsonEditor kind="dict" :model-value="extra" :aria-label="extrasLabel" @update:model-value="setExtra" />
      </div>
    </div>
  </div>
</template>

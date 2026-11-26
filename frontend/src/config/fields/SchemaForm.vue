<script>
import { defineComponent, computed, inject, provide, reactive } from 'vue'
import { isEmpty } from '../useConfig.js'
import { FORM_SCOPE } from './scope.js'
import { applies, selected, holds } from './when.js'

export { applies, selected }

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
//   spec.selector  a switch between alternative sets of fields (one entity or
//                  import + export): never written to the file; its value is
//                  the choice whose fields are set, else what was picked here,
//                  else its default
// A field that holds a value is always shown, so it can be seen and cleared;
// changing a choice drops the values of the fields that no longer apply.

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
    // Choices of the selectors made here while none of their fields is set.
    const picked = reactive({})
    const selectorNames = computed(() => Object.keys(props.specs).filter(k => props.specs[k].selector))
    function withSelectors(vals) {
      const out = { ...props.context, ...vals }
      for (const k of selectorNames.value) out[k] = selected(k, vals, props.specs, picked[k])
      return out
    }
    const shown = computed(() => withSelectors(values.value))
    function emitValues(next) {
      const cleaned = Object.fromEntries(Object.entries(next).filter(([, v]) => !isEmpty(v) || v === false || v === 0))
      emit('update:modelValue', Object.keys(cleaned).length ? cleaned : undefined)
    }
    function set(key, v) {
      const selector = props.specs[key]?.selector
      const next = { ...values.value }
      if (selector) picked[key] = v ?? props.specs[key].default
      else next[key] = v
      // Settings that only applied to the previous choice go with it.
      const now = selector ? { ...withSelectors(next), [key]: picked[key] } : withSelectors(next)
      for (const [name, spec] of Object.entries(props.specs)) {
        if (spec.when && key in spec.when && !applies(spec, now, props.specs)) delete next[name]
      }
      emitValues(next)
    }
    function setExtra(obj) {
      const known = Object.fromEntries(Object.entries(values.value).filter(([k]) => k in props.specs))
      emitValues({ ...known, ...(obj ?? {}) })
    }
    const isSet = (name) => !props.specs[name]?.selector && holds(values.value[name])
    const visible = computed(() => Object.entries(props.specs).filter(([name, spec]) =>
      isSet(name) || (!spec.legacy && applies(spec, shown.value, props.specs))))
    // A selector shows its derived value; everything else the file's.
    const valueOf = (name) => (props.specs[name]?.selector ? shown.value[name] : values.value[name])
    const main = computed(() => visible.value.filter(([, spec]) => !spec.advanced))
    const advanced = computed(() => visible.value.filter(([, spec]) => spec.advanced))
    const advancedSet = computed(() => advanced.value.filter(([name]) => isSet(name)).length)
    return { values, extra, set, setExtra, main, advanced, advancedSet, valueOf }
  },
})
</script>

<template>
  <div class="cfg-schema-form">
    <div v-if="main.length" class="cfg-grid">
      <SchemaField v-for="[name, spec] in main" :key="keyPrefix + name" :name="name" :spec="spec" :show-key="showKeys"
                   :model-value="valueOf(name)" :self-id="selfId" @update:model-value="set(name, $event)" />
    </div>
    <details v-if="advanced.length" class="cfg-advanced" :open="advancedSet > 0">
      <summary>More settings<span v-if="advancedSet" class="cfg-count">{{ advancedSet }} set</span></summary>
      <div class="cfg-grid">
        <SchemaField v-for="[name, spec] in advanced" :key="keyPrefix + name" :name="name" :spec="spec" :show-key="showKeys"
                     :model-value="valueOf(name)" :self-id="selfId" @update:model-value="set(name, $event)" />
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

<script>
import { defineComponent, computed } from 'vue'
import { editorFor, WIDE } from './FieldInput.vue'

// One labelled form field: the spec's label, the editor for the spec, and a
// hint built from the description and limits that ends with the config.yaml key.
export default defineComponent({
  name: 'SchemaField',
  props: {
    name:       { type: String, required: true },
    spec:       { type: Object, required: true },
    modelValue: { default: undefined },
    selfId:     { type: String, default: '' },
    showKey:    { type: Boolean, default: true },   // name the config.yaml key in the hint
  },
  emits: ['update:modelValue'],
  setup(props) {
    const editor = computed(() => editorFor(props.spec, props.modelValue))
    const wide = computed(() => WIDE.has(editor.value))
    const hint = computed(() => {
      const s = props.spec
      const parts = []
      if (s.description) parts.push(s.description)
      const lo = s.minimum != null ? `${s.exclusive_minimum ? '>' : '≥'} ${s.minimum}` : null
      const hi = s.maximum != null ? `≤ ${s.maximum}` : null
      if (lo || hi) parts.push([lo, hi].filter(Boolean).join(', '))
      return parts.join(' · ')
    })
    // Compound editors are a group of controls, not one control to label.
    const group = computed(() => wide.value || ['secret', 'entity', 'entity-value', 'time', 'month_day', 'weekdays'].includes(editor.value))
    return { editor, wide, hint, group }
  },
})
</script>

<template>
  <div :class="['cfg-field', { 'cfg-wide': wide }]">
    <component :is="group ? 'div' : 'label'" class="cfg-label" :role="group ? 'group' : null" :aria-label="group ? (spec.label || name) : null">
      <span class="cfg-name">{{ spec.label || name }}<span v-if="spec.required" class="cfg-req" title="required">*</span>
        <span v-if="spec.restart" class="pill pill-warn cfg-flag">needs a restart</span>
      </span>
      <span v-if="wide" class="cfg-hint">{{ hint }}<template v-if="hint && showKey"> · </template><code v-if="showKey" class="cfg-key">{{ name }}</code></span>
      <FieldInput :spec="spec" :name="name" :model-value="modelValue" :self-id="selfId" :aria-label="group ? (spec.label || name) : null"
                  @update:model-value="$emit('update:modelValue', $event)" />
    </component>
    <div v-if="!wide && (hint || showKey)" class="cfg-hint">{{ hint }}<template v-if="hint && showKey"> · </template><code v-if="showKey" class="cfg-key">{{ name }}</code></div>
  </div>
</template>

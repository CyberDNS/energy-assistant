<script>
import { defineComponent, computed, inject, ref, watch } from 'vue'
import { isEmpty } from '../useConfig.js'
import { FORM_SCOPE } from './scope.js'

// A mapping with free keys (name → value): one row per pair.  `valueSpec`
// is the spec of each value; `keySpec` the spec of each key (with choices:
// a dropdown of the keys not used yet; with choices_from: the values of a
// field of the items of a list elsewhere in the form, e.g. the period names).
// `itemLabel` names one pair in the buttons ("+ Add price").
export default defineComponent({
  name: 'MapEditor',
  props: {
    valueSpec:  { type: Object, default: () => ({ kind: 'any' }) },
    keySpec:    { type: Object, default: null },
    modelValue: { default: undefined },
    keyLabel:   { type: String, default: 'name' },
    itemLabel:  { type: String, default: 'entry' },
    valueLabel: { type: String, default: 'value' },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    // Rows are kept locally so a pair can be typed key first, value second.
    const rows = ref([])
    const asObject = (list) => Object.fromEntries(
      list.filter(r => r.key.trim() && !(isEmpty(r.value) && r.value !== false && r.value !== 0))
        .map(r => [r.key.trim(), r.value]))
    watch(() => props.modelValue, (v) => {
      const incoming = v && typeof v === 'object' ? v : {}
      if (JSON.stringify(asObject(rows.value)) !== JSON.stringify(incoming)) {
        rows.value = Object.entries(incoming).map(([key, value]) => ({ key, value }))
      }
    }, { immediate: true })
    function push() {
      const obj = asObject(rows.value)
      emit('update:modelValue', Object.keys(obj).length ? obj : undefined)
    }
    const remove = (i) => { rows.value.splice(i, 1); push() }
    const add = () => rows.value.push({ key: '', value: undefined })
    const scope = inject(FORM_SCOPE, null)
    const choices = computed(() => {
      const from = props.keySpec?.choices_from
      if (!from) return props.keySpec?.choices ?? null
      const list = scope?.lookup(from.list, Array.isArray)
      return (Array.isArray(list) ? list : []).map(item => item?.[from.field])
        .filter(v => v != null && v !== '').map(String)
    })
    const keyChoices = (i) => (choices.value ?? [])
      .filter(c => c === rows.value[i].key || !rows.value.some(r => r.key === c))
    const keyLabelOf = (c) => props.keySpec?.choice_labels?.[c] ?? c
    const keyName = computed(() => (props.keySpec?.label || props.keyLabel).toLowerCase())
    return { rows, push, remove, add, choices, keyChoices, keyLabelOf, keyName }
  },
})
</script>

<template>
  <div class="cfg-list-editor">
    <div v-for="(row, i) in rows" :key="i" class="cfg-list-row cfg-map-row">
      <select v-if="choices" v-model="row.key" :aria-label="keyName + ' ' + (i + 1)" @change="push">
        <option value="" disabled>— choose {{ keyName }}</option>
        <option v-for="c in keyChoices(i)" :key="c" :value="c">{{ keyLabelOf(c) }}</option>
        <option v-if="row.key && !choices.includes(row.key)" :value="row.key">{{ row.key }} (unknown)</option>
      </select>
      <EntityPicker v-else-if="keySpec?.entity" :model-value="row.key || undefined" :domains="keySpec.domains ?? []"
                    :aria-label="keyLabel + ' ' + (i + 1)" @update:model-value="row.key = $event ?? ''; push()" />
      <input v-else type="text" v-model="row.key" :placeholder="keyLabel" :aria-label="keyLabel + ' ' + (i + 1)"
             autocapitalize="off" spellcheck="false" @input="push" />
      <span class="cfg-map-arrow">→</span>
      <FieldInput :spec="valueSpec" :model-value="row.value" :aria-label="valueLabel + ' ' + (i + 1)"
                  @update:model-value="row.value = $event; push()" />
      <button type="button" class="btn btn-danger btn-icon" :title="'Remove ' + itemLabel" :aria-label="'Remove ' + itemLabel + ' ' + (i + 1)" @click="remove(i)">✕</button>
    </div>
    <button type="button" class="btn btn-neutral cfg-mini" @click="add">+ Add {{ itemLabel }}</button>
  </div>
</template>

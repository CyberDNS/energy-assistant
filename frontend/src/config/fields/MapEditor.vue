<script>
import { defineComponent, ref, watch } from 'vue'
import { isEmpty } from '../useConfig.js'

// A mapping with free keys (name → value): one row per pair.  `valueSpec`
// is the spec of each value; `keySpec` the spec of each key (with choices:
// a dropdown of the keys not used yet).
export default defineComponent({
  name: 'MapEditor',
  props: {
    valueSpec:  { type: Object, default: () => ({ kind: 'any' }) },
    keySpec:    { type: Object, default: null },
    modelValue: { default: undefined },
    keyLabel:   { type: String, default: 'name' },
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
    const keyChoices = (i) => (props.keySpec?.choices ?? [])
      .filter(c => c === rows.value[i].key || !rows.value.some(r => r.key === c))
    const keyLabelOf = (c) => props.keySpec?.choice_labels?.[c] ?? c
    return { rows, push, remove, add, keyChoices, keyLabelOf }
  },
})
</script>

<template>
  <div class="cfg-list-editor">
    <div v-for="(row, i) in rows" :key="i" class="cfg-list-row cfg-map-row">
      <select v-if="keySpec?.choices" v-model="row.key" :aria-label="keyLabel + ' ' + (i + 1)" @change="push">
        <option value="" disabled>— choose —</option>
        <option v-for="c in keyChoices(i)" :key="c" :value="c">{{ keyLabelOf(c) }}</option>
      </select>
      <EntityPicker v-else-if="keySpec?.entity" :model-value="row.key || undefined" :domains="keySpec.domains ?? []"
                    :aria-label="keyLabel + ' ' + (i + 1)" @update:model-value="row.key = $event ?? ''; push()" />
      <input v-else type="text" v-model="row.key" :placeholder="keyLabel" :aria-label="keyLabel + ' ' + (i + 1)"
             autocapitalize="off" spellcheck="false" @input="push" />
      <span class="cfg-map-arrow">→</span>
      <FieldInput :spec="valueSpec" :model-value="row.value" :aria-label="valueLabel + ' ' + (i + 1)"
                  @update:model-value="row.value = $event; push()" />
      <button type="button" class="btn btn-danger btn-icon" title="Remove" aria-label="Remove" @click="remove(i)">✕</button>
    </div>
    <button type="button" class="btn btn-neutral cfg-mini" @click="add">+ Add</button>
  </div>
</template>

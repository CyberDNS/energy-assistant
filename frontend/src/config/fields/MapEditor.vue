<script>
import { defineComponent, ref, watch } from 'vue'
import { isEmpty } from '../useConfig.js'

// A mapping with free keys (name → value): one row per pair.  `valueSpec`
// is the spec of each value.
export default defineComponent({
  name: 'MapEditor',
  props: {
    valueSpec:  { type: Object, default: () => ({ kind: 'any' }) },
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
    return { rows, push, remove, add }
  },
})
</script>

<template>
  <div class="cfg-list-editor">
    <div v-for="(row, i) in rows" :key="i" class="cfg-list-row cfg-map-row">
      <input type="text" v-model="row.key" :placeholder="keyLabel" :aria-label="keyLabel + ' ' + (i + 1)"
             autocapitalize="off" spellcheck="false" @input="push" />
      <span class="cfg-map-arrow">→</span>
      <FieldInput :spec="valueSpec" :model-value="row.value" :aria-label="valueLabel + ' ' + (i + 1)"
                  @update:model-value="row.value = $event; push()" />
      <button type="button" class="btn btn-danger btn-icon" title="Remove" aria-label="Remove" @click="remove(i)">✕</button>
    </div>
    <button type="button" class="btn btn-neutral cfg-mini" @click="add">+ Add</button>
  </div>
</template>

<script>
import { defineComponent, ref, watch } from 'vue'

// An ordered list of simple values (texts, numbers, devices, entities): one
// row per item with move and remove buttons.  `item` is the spec of an item.
export default defineComponent({
  name: 'ListEditor',
  props: {
    item:       { type: Object, default: () => ({ kind: 'str' }) },
    modelValue: { default: undefined },
    selfId:     { type: String, default: '' },
    addLabel:   { type: String, default: 'Add' },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    // Rows are kept locally so an added row can stay empty until it is filled.
    const rows = ref([])
    const filled = (list) => list.filter(v => v !== undefined && v !== '')
    watch(() => props.modelValue, (v) => {
      const incoming = Array.isArray(v) ? v : []
      if (JSON.stringify(filled(rows.value)) !== JSON.stringify(incoming)) rows.value = [...incoming]
    }, { immediate: true })
    function push() {
      const list = filled(rows.value)
      emit('update:modelValue', list.length ? list : undefined)
    }
    const setRow = (i, v) => { rows.value[i] = v; push() }
    const remove = (i) => { rows.value.splice(i, 1); push() }
    const add = () => rows.value.push(undefined)
    function move(i, by) {
      const j = i + by
      if (j < 0 || j >= rows.value.length) return
      ;[rows.value[i], rows.value[j]] = [rows.value[j], rows.value[i]]
      push()
    }
    return { rows, setRow, remove, add, move }
  },
})
</script>

<template>
  <div class="cfg-list-editor">
    <div v-for="(row, i) in rows" :key="i" class="cfg-list-row">
      <span class="cfg-list-no">{{ i + 1 }}</span>
      <FieldInput :spec="item" :model-value="row" :self-id="selfId" :aria-label="'Item ' + (i + 1)"
                  @update:model-value="setRow(i, $event)" />
      <button type="button" class="btn btn-neutral btn-icon" :disabled="i === 0" title="Move up" aria-label="Move up" @click="move(i, -1)">↑</button>
      <button type="button" class="btn btn-neutral btn-icon" :disabled="i === rows.length - 1" title="Move down" aria-label="Move down" @click="move(i, 1)">↓</button>
      <button type="button" class="btn btn-danger btn-icon" title="Remove" aria-label="Remove" @click="remove(i)">✕</button>
    </div>
    <button type="button" class="btn btn-neutral cfg-mini" @click="add">+ {{ addLabel }}</button>
  </div>
</template>

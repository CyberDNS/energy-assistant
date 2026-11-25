<script>
import { defineComponent, computed } from 'vue'
import { capitalize, itemLabelOf, summaryOf } from './items.js'

// A list of records whose fields hold other lists (a period with its time
// windows, a season with its prices): one card per record, titled with its
// number and summary, with the record's own form inside.  Every button names
// what it adds or removes ("+ Add period", "Remove period"), so nested lists
// stay apart.
//
// Item fields with widget "exclusive" (a yes/no only one item may have, e.g.
// the period for all other times) are not part of the cards: one dropdown
// below them picks the item.
export default defineComponent({
  name: 'CardListEditor',
  props: {
    spec:       { type: Object, required: true },   // the list's spec (items.fields = the record)
    modelValue: { default: undefined },
    selfId:     { type: String, default: '' },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const rows = computed(() => (Array.isArray(props.modelValue) ? props.modelValue : []))
    const itemSpec = computed(() => props.spec.items ?? {})
    const item = computed(() => itemLabelOf(props.spec))
    const exclusive = computed(() => Object.entries(itemSpec.value.fields ?? {}).filter(([, s]) => s.widget === 'exclusive'))
    const formSpecs = computed(() => Object.fromEntries(
      Object.entries(itemSpec.value.fields ?? {}).filter(([, s]) => s.widget !== 'exclusive')))
    const emitRows = (next) => emit('update:modelValue', next.length ? next : undefined)
    const titleOf = (row, i) => summaryOf(itemSpec.value, row)
    const nameOf = (row, i) => (row?.name ? String(row.name) : `${capitalize(item.value)} ${i + 1}`)

    // The card's form edits the record without the exclusive fields.
    const formValue = (row) => Object.fromEntries(Object.entries(row ?? {}).filter(([k]) => !exclusive.value.some(([n]) => n === k)))
    function setRow(i, v) {
      emitRows(rows.value.map((r, n) => {
        if (n !== i) return r
        const kept = Object.fromEntries(Object.entries(r ?? {}).filter(([k]) => exclusive.value.some(([n]) => n === k)))
        return { ...(v ?? {}), ...kept }
      }))
    }
    const remove = (i) => emitRows(rows.value.filter((_, n) => n !== i))
    const add = () => emit('update:modelValue', [...rows.value, {}])
    function move(i, by) {
      const j = i + by
      if (j < 0 || j >= rows.value.length) return
      const next = [...rows.value]
      ;[next[i], next[j]] = [next[j], next[i]]
      emitRows(next)
    }

    const chosen = (key) => rows.value.findIndex(r => r?.[key])
    function choose(key, index) {
      emitRows(rows.value.map((r, n) => {
        const next = { ...(r ?? {}) }
        if (String(n) === index) next[key] = true
        else delete next[key]
        return next
      }))
    }
    return { rows, itemSpec, item, exclusive, formSpecs, titleOf, nameOf, formValue, setRow, remove, add, move,
             chosen, choose, capitalize }
  },
})
</script>

<template>
  <div class="cfg-cards">
    <section v-for="(row, i) in rows" :key="i" class="cfg-card" :aria-label="capitalize(item) + ' ' + (i + 1)">
      <header class="cfg-card-head">
        <span class="cfg-card-title">{{ capitalize(item) }} {{ i + 1 }}<span v-if="titleOf(row, i)" class="cfg-card-summary"> · {{ titleOf(row, i) }}</span></span>
        <span class="ev-actions-spacer"></span>
        <template v-if="rows.length > 1">
          <button type="button" class="btn btn-neutral btn-icon" :disabled="i === 0" :title="'Move ' + item + ' up'" :aria-label="'Move ' + item + ' ' + (i + 1) + ' up'" @click="move(i, -1)">↑</button>
          <button type="button" class="btn btn-neutral btn-icon" :disabled="i === rows.length - 1" :title="'Move ' + item + ' down'" :aria-label="'Move ' + item + ' ' + (i + 1) + ' down'" @click="move(i, 1)">↓</button>
        </template>
        <button type="button" class="btn btn-danger cfg-mini" :aria-label="'Remove ' + item + ' ' + (i + 1)" @click="remove(i)">Remove {{ item }}</button>
      </header>
      <SchemaForm :specs="formSpecs" :model-value="formValue(row)" :self-id="selfId" :show-keys="false"
                  @update:model-value="setRow(i, $event)" />
    </section>
    <button type="button" class="btn btn-neutral cfg-card-add" @click="add">+ Add {{ item }}</button>
    <div v-for="[key, s] in exclusive" :key="key" class="cfg-field cfg-card-exclusive">
      <label class="cfg-label"><span class="cfg-name">{{ s.label || key }}</span>
        <select :value="String(chosen(key))" @change="choose(key, $event.target.value)">
          <option value="-1">— none</option>
          <option v-for="(row, i) in rows" :key="i" :value="String(i)">{{ nameOf(row, i) }}</option>
        </select>
      </label>
      <div v-if="s.description" class="cfg-hint">{{ s.description }} · <code class="cfg-key">{{ key }}: true</code></div>
    </div>
  </div>
</template>

<script>
import { defineComponent, computed } from 'vue'
import { isEmpty } from '../useConfig.js'
import { applies } from './SchemaForm.vue'

// A list of records as a table: one column per field of `fields`
// ({ key: spec }), one row per record.  Used for charge curves, PV planes,
// time windows, exclusion rules.  `itemLabel` names one row in the buttons
// ("+ Add time window").  A field whose `when` does not hold for its row is
// left empty and dropped from the row (e.g. from/to of an all-day window).
export default defineComponent({
  name: 'TableEditor',
  props: {
    fields:     { type: Object, required: true },
    modelValue: { default: undefined },
    selfId:     { type: String, default: '' },
    itemLabel:  { type: String, default: 'row' },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const rows = computed(() => (Array.isArray(props.modelValue) ? props.modelValue : []))
    const emitRows = (next) => emit('update:modelValue', next.length ? next : undefined)
    const shows = (spec, row) => applies(spec, row ?? {}, props.fields)
    function setCell(i, key, v) {
      emitRows(rows.value.map((r, n) => {
        if (n !== i) return r
        const next = { ...r, [key]: v }
        if (isEmpty(v) && v !== false && v !== 0) delete next[key]
        for (const [name, spec] of Object.entries(props.fields)) {
          if (spec.when && key in spec.when && !applies(spec, next, props.fields)) delete next[name]
        }
        return next
      }))
    }
    const remove = (i) => emitRows(rows.value.filter((_, n) => n !== i))
    const add = () => emit('update:modelValue', [...rows.value, {}])
    return { rows, shows, setCell, remove, add }
  },
})
</script>

<template>
  <div class="cfg-table-wrap">
    <table v-if="rows.length" class="cfg-table">
      <thead>
        <tr>
          <th v-for="(spec, key) in fields" :key="key" :title="spec.description || null">
            {{ spec.label || key }}<span v-if="spec.required" class="cfg-req">*</span>
          </th>
          <th class="cfg-table-act"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, i) in rows" :key="i">
          <td v-for="(spec, key) in fields" :key="key" :data-label="spec.label || key"
              :class="{ 'cfg-table-off': !shows(spec, row) && row?.[key] == null }">
            <FieldInput v-if="shows(spec, row) || row?.[key] != null" :spec="spec" :model-value="row?.[key]" :self-id="selfId"
                        :aria-label="(spec.label || key) + ', ' + itemLabel + ' ' + (i + 1)"
                        @update:model-value="setCell(i, key, $event)" />
            <span v-else class="cfg-hint">—</span>
          </td>
          <td class="cfg-table-act">
            <button type="button" class="btn btn-danger btn-icon" :title="'Remove ' + itemLabel" :aria-label="'Remove ' + itemLabel + ' ' + (i + 1)" @click="remove(i)">✕</button>
          </td>
        </tr>
      </tbody>
    </table>
    <button type="button" class="btn btn-neutral cfg-mini" @click="add">+ Add {{ itemLabel }}</button>
  </div>
</template>

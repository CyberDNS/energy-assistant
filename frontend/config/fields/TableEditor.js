import { defineComponent, computed } from 'vue'
import { isEmpty } from '../useConfig.js'

// A list of records as a table: one column per field of `fields`
// ({ key: spec }), one row per record.  Used for charge curves, PV planes,
// load profiles, exclusion rules.
export default defineComponent({
  name: 'TableEditor',
  props: {
    fields:     { type: Object, required: true },
    modelValue: { default: undefined },
    selfId:     { type: String, default: '' },
    addLabel:   { type: String, default: 'Add row' },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const rows = computed(() => (Array.isArray(props.modelValue) ? props.modelValue : []))
    const emitRows = (next) => emit('update:modelValue', next.length ? next : undefined)
    function setCell(i, key, v) {
      emitRows(rows.value.map((r, n) => {
        if (n !== i) return r
        const next = { ...r, [key]: v }
        if (isEmpty(v) && v !== false && v !== 0) delete next[key]
        return next
      }))
    }
    const remove = (i) => emitRows(rows.value.filter((_, n) => n !== i))
    const add = () => emit('update:modelValue', [...rows.value, {}])
    return { rows, setCell, remove, add }
  },
  template: `
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
            <td v-for="(spec, key) in fields" :key="key" :data-label="spec.label || key">
              <FieldInput :spec="spec" :model-value="row?.[key]" :self-id="selfId" :aria-label="(spec.label || key) + ', row ' + (i + 1)"
                          @update:model-value="setCell(i, key, $event)" />
            </td>
            <td class="cfg-table-act">
              <button type="button" class="btn btn-danger btn-icon" title="Remove row" aria-label="Remove row" @click="remove(i)">✕</button>
            </td>
          </tr>
        </tbody>
      </table>
      <button type="button" class="btn btn-neutral cfg-mini" @click="add">+ {{ addLabel }}</button>
    </div>
  `,
})

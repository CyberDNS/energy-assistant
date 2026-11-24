<script>
import { defineComponent, computed } from 'vue'

// A single value: text, number, yes/no or one of a few choices, depending on
// the spec.  Emits undefined when empty or when the default is chosen (the
// key is then left out of the file and the default applies).
export default defineComponent({
  name: 'ScalarInput',
  props: {
    spec:       { type: Object, required: true },
    modelValue: { default: undefined },
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],

  setup(props, { emit }) {
    const kind = computed(() => {
      const s = props.spec
      if (s.choices) return 'choice'
      if (s.kind === 'bool') return 'bool'
      if (s.kind === 'float' || s.kind === 'int') return 'number'
      return 'text'
    })
    const placeholder = computed(() => (props.spec.default != null ? `default: ${props.spec.default}` : ''))
    const set = (v) => emit('update:modelValue', v)
    // Dropdowns offer the real values only; the default is marked and shown
    // while nothing is set.  Picking it leaves the key out of the file.
    const pick = (v) => set(v === '' || v === props.spec.default ? undefined : v)

    function setNumber(text) {
      if (text === '' || text == null) return set(undefined)
      const n = Number(text)
      set(Number.isNaN(n) ? text : n)
    }
    // kind "any": numbers stay numbers, lists are written as JSON.
    const text = computed(() => {
      const v = props.modelValue
      if (v == null) return ''
      return typeof v === 'object' ? JSON.stringify(v) : String(v)
    })
    function setText(t) {
      if (t === '') return set(undefined)
      if (props.spec.kind === 'any') {
        if (/^-?\d+(\.\d+)?$/.test(t.trim())) return set(Number(t))
        if (/^[[{]/.test(t.trim())) { try { return set(JSON.parse(t)) } catch { /* keep as text */ } }
      }
      set(t)
    }
    return { kind, placeholder, set, pick, setNumber, text, setText }
  },
})
</script>

<template>
  <select v-if="kind === 'choice'" :value="modelValue ?? spec.default ?? ''" :aria-label="ariaLabel"
          @change="pick($event.target.value)">
    <option v-if="spec.default == null" value="">{{ spec.required ? '— choose' : '— not set' }}</option>
    <option v-for="c in spec.choices" :key="c" :value="c">{{ spec.choice_labels?.[c] ?? c }}{{ c === spec.default ? ' (default)' : '' }}</option>
    <option v-if="modelValue != null && !spec.choices.includes(modelValue)" :value="modelValue">{{ modelValue }} (invalid)</option>
  </select>
  <select v-else-if="kind === 'bool'" :value="String(modelValue ?? spec.default ?? '')" :aria-label="ariaLabel"
          @change="pick($event.target.value === '' ? '' : $event.target.value === 'true')">
    <option v-if="spec.default == null" value="">— not set</option>
    <option value="true">yes{{ spec.default === true ? ' (default)' : '' }}</option>
    <option value="false">no{{ spec.default === false ? ' (default)' : '' }}</option>
  </select>
  <input v-else-if="kind === 'number'" type="number" step="any" inputmode="decimal" :aria-label="ariaLabel"
         :value="modelValue ?? ''" :placeholder="placeholder" :min="spec.minimum" :max="spec.maximum"
         @input="setNumber($event.target.value)" />
  <input v-else type="text" :value="text" :placeholder="placeholder" :aria-label="ariaLabel"
         autocapitalize="off" spellcheck="false" @input="setText($event.target.value)" />
</template>

import { defineComponent, ref, watch } from 'vue'

// Raw editor (JSON text) for values that have no dedicated editor.
export default defineComponent({
  name: 'JsonEditor',
  props: {
    kind:       { type: String, default: 'dict' },   // 'dict' | 'list' | 'any'
    modelValue: { default: undefined },
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const text = ref('')
    const error = ref(null)
    const pretty = (v) => (v == null ? '' : JSON.stringify(v, null, 1))
    watch(() => props.modelValue, (v) => {
      let current
      try { current = text.value.trim() ? JSON.parse(text.value) : undefined } catch { return }
      if (JSON.stringify(current) !== JSON.stringify(v ?? undefined)) text.value = pretty(v)
    }, { immediate: true })
    function onInput(t) {
      text.value = t
      if (!t.trim()) { error.value = null; emit('update:modelValue', undefined); return }
      try {
        const v = JSON.parse(t)
        const isMap = v !== null && typeof v === 'object' && !Array.isArray(v)
        if (props.kind === 'list' && !Array.isArray(v)) { error.value = 'Expected a list: [ … ]'; return }
        if (props.kind === 'dict' && !isMap) { error.value = 'Expected a mapping: { … }'; return }
        error.value = null
        emit('update:modelValue', v)
      } catch (e) { error.value = 'Not valid JSON: ' + e.message }
    }
    return { text, error, onInput }
  },
  template: `
    <div>
      <textarea :rows="Math.min(10, Math.max(2, text.split('\\n').length))" spellcheck="false" :value="text" :aria-label="ariaLabel"
                :placeholder="kind === 'list' ? '[ … ]' : '{ … }'" @input="onInput($event.target.value)"></textarea>
      <div v-if="error" class="ev-error">{{ error }}</div>
    </div>
  `,
})

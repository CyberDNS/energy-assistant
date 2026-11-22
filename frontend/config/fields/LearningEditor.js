import { defineComponent, computed, ref } from 'vue'
import { useConfig } from '../useConfig.js'

// A device's `learning:` — on (the default), off, or on with options.  The
// options depend on what is learned: a consumption forecast (`consumer`) or a
// thermal model (`thermal_load`).
export default defineComponent({
  name: 'LearningEditor',
  props: {
    kind:       { type: String, default: '' },   // 'consumer' | 'thermal_load' | ''
    modelValue: { default: undefined },          // true | false | { options } | undefined
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const cfg = useConfig()
    const specs = computed(() => cfg.schema.value.learning[props.kind] ?? null)
    const isOptions = computed(() => props.modelValue != null && typeof props.modelValue === 'object')
    // "on, with options" stays open while no option is filled in yet (that is
    // still plain "on" in the file).
    const open = ref(false)
    const mode = computed(() => (props.modelValue === false ? 'off' : (isOptions.value || open.value) ? 'options' : 'on'))
    function setMode(m) {
      open.value = m === 'options'
      emit('update:modelValue', m === 'off' ? false : (m === 'options' && isOptions.value) ? props.modelValue : undefined)
    }
    const setOptions = (o) => emit('update:modelValue', o)
    return { specs, mode, setMode, setOptions }
  },
  template: `
    <div class="cfg-plugin">
      <div class="cfg-grid">
        <div class="cfg-field">
          <select :value="mode" aria-label="Learning" @change="setMode($event.target.value)">
            <option value="on">on (default)</option>
            <option v-if="specs" value="options">on, with options</option>
            <option value="off">off</option>
          </select>
        </div>
      </div>
      <SchemaForm v-if="mode === 'options' && specs" :specs="specs" :model-value="modelValue === true ? undefined : modelValue" extras @update:model-value="setOptions" />
    </div>
  `,
})

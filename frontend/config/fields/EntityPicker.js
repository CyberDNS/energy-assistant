import { defineComponent, computed } from 'vue'
import { useConfig } from '../useConfig.js'

let counter = 0

// Text input for a Home Assistant entity id with suggestions from HA.
export default defineComponent({
  name: 'EntityPicker',
  props: {
    modelValue:  { default: undefined },
    placeholder: { type: String, default: 'sensor.…' },
    ariaLabel:   { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props) {
    const { entities } = useConfig()
    const listId = `entities-${++counter}`
    const name = computed(() => entities.value.find(e => e.entity_id === props.modelValue)?.name ?? '')
    const unknown = computed(() => !!props.modelValue && entities.value.length > 0
      && !entities.value.some(e => e.entity_id === props.modelValue))
    return { entities, listId, name, unknown }
  },
  template: `
    <span class="cfg-entity">
      <input type="text" :value="modelValue ?? ''" :list="listId" :placeholder="placeholder" :aria-label="ariaLabel"
             autocapitalize="off" spellcheck="false"
             @input="$emit('update:modelValue', $event.target.value.trim() || undefined)" />
      <datalist :id="listId">
        <option v-for="e in entities" :key="e.entity_id" :value="e.entity_id">{{ e.name }}</option>
      </datalist>
      <span v-if="name" class="cfg-entity-name">{{ name }}</span>
      <span v-else-if="unknown" class="cfg-entity-name warn">not found in Home Assistant</span>
    </span>
  `,
})

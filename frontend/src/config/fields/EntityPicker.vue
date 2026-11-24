<script>
import { defineComponent, computed } from 'vue'
import { useConfig } from '../useConfig.js'

let counter = 0

// Text input for a Home Assistant entity id with suggestions from HA.
export default defineComponent({
  name: 'EntityPicker',
  props: {
    modelValue:  { default: undefined },
    placeholder: { type: String, default: null },
    // Entity domains to suggest (empty: all).
    domains:     { type: Array, default: () => [] },
    ariaLabel:   { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props) {
    const { entities: all } = useConfig()
    const entities = computed(() => props.domains.length
      ? all.value.filter(e => props.domains.includes(e.entity_id.split('.', 1)[0]))
      : all.value)
    const hint = computed(() => props.placeholder ?? `${props.domains[0] ?? 'sensor'}.…`)
    const listId = `entities-${++counter}`
    const name = computed(() => all.value.find(e => e.entity_id === props.modelValue)?.name ?? '')
    const unknown = computed(() => !!props.modelValue && all.value.length > 0
      && !all.value.some(e => e.entity_id === props.modelValue))
    return { entities, hint, listId, name, unknown }
  },
})
</script>

<template>
  <span class="cfg-entity">
    <input type="text" :value="modelValue ?? ''" :list="listId" :placeholder="hint" :aria-label="ariaLabel"
           autocapitalize="off" spellcheck="false"
           @input="$emit('update:modelValue', $event.target.value.trim() || undefined)" />
    <datalist :id="listId">
      <option v-for="e in entities" :key="e.entity_id" :value="e.entity_id">{{ e.name }}</option>
    </datalist>
    <span v-if="name" class="cfg-entity-name">{{ name }}</span>
    <span v-else-if="unknown" class="cfg-entity-name warn">not found in Home Assistant</span>
  </span>
</template>

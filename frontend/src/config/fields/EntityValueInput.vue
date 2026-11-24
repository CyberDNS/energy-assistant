<script>
import { defineComponent, computed, inject } from 'vue'
import { useConfig } from '../useConfig.js'
import { FORM_SCOPE } from './scope.js'

// A value that depends on an entity chosen in another field of the same (or
// an enclosing) form:
//   spec.options_from { entity, attribute }  one of the values listed in that
//                                            attribute (a select's options, a
//                                            climate's hvac_modes, …)
//   spec.attribute_of  entity-field          an attribute name of the entity
// Suggestions only: any text can be typed.
export default defineComponent({
  name: 'EntityValueInput',
  props: {
    spec:       { type: Object, required: true },
    modelValue: { default: undefined },
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props) {
    const { entities } = useConfig()
    const scope = inject(FORM_SCOPE, null)
    const entityKey = computed(() => props.spec.options_from?.entity ?? props.spec.attribute_of)
    const entityId = computed(() => scope?.lookup(entityKey.value))
    const entity = computed(() => entities.value.find(e => e.entity_id === entityId.value) ?? null)
    const items = computed(() => {
      const e = entity.value
      if (!e) return []
      if (props.spec.attribute_of) return (e.attributes ?? []).map(a => ({ value: a }))
      return (e.options?.[props.spec.options_from.attribute] ?? []).map(o => ({ value: o }))
    })
    const hint = computed(() => entity.value
      ? (items.value.length ? `Options of ${entity.value.name || entity.value.entity_id}` : '')
      : (entityId.value ? '' : `Choose the ${entityKey.value} first to see its options`))
    return { items, hint }
  },
})
</script>

<template>
  <span class="cfg-entity">
    <Combobox :model-value="modelValue" :items="items" :aria-label="ariaLabel"
              :placeholder="items.length ? 'Choose or type …' : ''"
              @update:model-value="$emit('update:modelValue', $event)" />
    <span v-if="hint" class="cfg-entity-name">{{ hint }}</span>
  </span>
</template>

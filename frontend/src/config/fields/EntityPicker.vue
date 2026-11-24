<script>
import { defineComponent, computed, ref } from 'vue'
import { useConfig } from '../useConfig.js'

// A Home Assistant entity: searchable by friendly name, entity id and area,
// showing each entity's current state.  Only compatible entities are offered:
// of the field's domains and, when the field names units, with one of them
// (entities of `unitFreeDomains` whatever their unit).  "Show all" lifts the
// filter, e.g. for a sensor without a unit.  Free text stays possible.
export default defineComponent({
  name: 'EntityPicker',
  props: {
    modelValue:  { default: undefined },
    placeholder: { type: String, default: null },
    // Entity domains to offer (empty: all).
    domains:     { type: Array, default: () => [] },
    // Units a fitting entity has (empty: any), and domains exempt from them.
    units:       { type: Array, default: () => [] },
    unitFreeDomains: { type: Array, default: () => [] },
    ariaLabel:   { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props) {
    const { entities: all } = useConfig()
    const stateOf = (e) => e.state == null ? '' : `${e.state}${e.unit ? ' ' + e.unit : ''}`
    const showAll = ref(false)
    const domainOf = (e) => e.entity_id.split('.', 1)[0]
    const units = computed(() => props.units.map(u => u.toLowerCase()))
    const fits = (e) => !units.value.length || props.unitFreeDomains.includes(domainOf(e))
      || units.value.includes(String(e.unit ?? '').toLowerCase())
    const inDomain = computed(() => all.value
      .filter(e => !props.domains.length || props.domains.includes(domainOf(e))))
    const compatible = computed(() => inDomain.value.filter(fits))
    const filtered = computed(() => units.value.length > 0 && compatible.value.length < inDomain.value.length)
    const items = computed(() => (showAll.value ? inDomain.value : compatible.value)
      .map(e => ({
        value: e.entity_id,
        label: e.name || e.entity_id,
        detail: e.area ? `${e.entity_id} · ${e.area}` : e.entity_id,
        meta: stateOf(e),
      })))
    const hint = computed(() => props.placeholder ?? `Search ${props.domains.length ? props.domains.slice(0, 2).join(' / ') : 'entities'} …`)
    const current = computed(() => all.value.find(e => e.entity_id === props.modelValue) ?? null)
    const summary = computed(() => {
      const e = current.value
      if (!e) return ''
      return [e.name, e.area, stateOf(e)].filter(Boolean).join(' · ')
    })
    const unknown = computed(() => !!props.modelValue && all.value.length > 0 && !current.value)
    // The chosen entity has another unit than the field expects.
    const misfit = computed(() => current.value && !fits(current.value)
      ? `unit ${current.value.unit || 'none'} — expected ${props.units.join(' / ')}` : '')
    return { items, hint, summary, unknown, misfit, showAll, filtered,
             hidden: computed(() => inDomain.value.length - compatible.value.length) }
  },
})
</script>

<template>
  <span class="cfg-entity">
    <Combobox :model-value="modelValue" :items="items" :placeholder="hint" :aria-label="ariaLabel"
              empty-text="No matching entity" @update:model-value="$emit('update:modelValue', $event)" />
    <span v-if="summary" class="cfg-entity-name">{{ summary }}</span>
    <span v-else-if="unknown" class="cfg-entity-name warn">not found in Home Assistant</span>
    <span v-if="misfit" class="cfg-entity-name warn">{{ misfit }}</span>
    <button v-if="filtered" type="button" class="cfg-entity-all" @click="showAll = !showAll">
      {{ showAll ? 'Only fitting entities' : `Show all (${hidden} more without a fitting unit)` }}
    </button>
  </span>
</template>

<script>
import { defineComponent, computed } from 'vue'
import { useConfig } from '../useConfig.js'

// Which editor a spec gets.  Everything that edits a config value goes
// through here, so a new kind of value needs one new editor component and one
// line in this function.
export function editorFor(spec, value) {
  const isObj = value != null && typeof value === 'object'
  if (spec.secret || (isObj && ('!secret' in value || '!hidden' in value))) return 'secret'
  if (spec.widget === 'load_profile') return 'profile'
  if (spec.widget?.startsWith('learning')) return 'learning'
  if (spec.plugin) return 'plugin'
  if (spec.ref) return 'device'
  if (spec.tariff) return 'tariff'
  if (spec.kind === 'list') {
    if (!spec.items) return 'json'
    return spec.items.fields ? 'table' : 'list'
  }
  if (spec.kind === 'dict') {
    if (spec.fields) return 'object'
    if (spec.values) return 'map'
    return 'json'
  }
  if (spec.entity) return 'entity'
  if (spec.options_from || spec.attribute_of) return 'entity-value'
  return 'scalar'
}

// Editors that need the full width of a form.
export const WIDE = new Set(['table', 'list', 'map', 'object', 'json', 'profile', 'plugin', 'learning'])

// The editor for one value, without label or hint (see SchemaField for those).
// The editor components are registered globally (fields/index.js) because
// they nest in each other.
export default defineComponent({
  name: 'FieldInput',
  props: {
    spec:       { type: Object, required: true },
    modelValue: { default: undefined },
    name:       { type: String, default: '' },
    selfId:     { type: String, default: '' },    // device being edited (left out of device pickers)
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const cfg = useConfig()
    const editor = computed(() => editorFor(props.spec, props.modelValue))
    const deviceOptions = computed(() => cfg.devices.value
      .filter(d => d.id !== props.selfId && (props.spec.ref.includes('*') || props.spec.ref.includes(d.category)))
      .map(d => ({ value: d.id, label: `${d.label} (${d.id})` })))
    const tariffOptions = computed(() => cfg.tariffs.value.map(t => ({ value: t, label: t })))
    const set = (v) => emit('update:modelValue', v)
    return { cfg, editor, deviceOptions, tariffOptions, set }
  },
})
</script>

<template>
  <SecretPicker v-if="editor === 'secret'" :model-value="modelValue" :suggest="name" :aria-label="ariaLabel" @update:model-value="set" />
  <LoadProfileEditor v-else-if="editor === 'profile'" :model-value="modelValue" @update:model-value="set" />
  <LearningEditor v-else-if="editor === 'learning'" :model-value="modelValue" :kind="spec.widget.split(':')[1]" @update:model-value="set" />
  <PluginEditor v-else-if="editor === 'plugin'" :kind="spec.plugin" scope="device" optional :model-value="modelValue" @update:model-value="set" />
  <OptionSelect v-else-if="editor === 'device'" :options="deviceOptions" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <OptionSelect v-else-if="editor === 'tariff'" :options="tariffOptions" :default-value="cfg.defaultTariff.value" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <TableEditor v-else-if="editor === 'table'" :fields="spec.items.fields" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <ListEditor v-else-if="editor === 'list'" :item="spec.items" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <MapEditor v-else-if="editor === 'map'" :value-spec="spec.values" :key-spec="spec.keys ?? null" :model-value="modelValue" @update:model-value="set" />
  <SchemaForm v-else-if="editor === 'object'" class="cfg-nested" :specs="spec.fields" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <JsonEditor v-else-if="editor === 'json'" :kind="spec.kind" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <EntityValueInput v-else-if="editor === 'entity-value'" :spec="spec" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <EntityPicker v-else-if="editor === 'entity'" :model-value="modelValue" :domains="spec.domains ?? []"
                :units="spec.units ?? []" :unit-free-domains="spec.unit_free_domains ?? []" :aria-label="ariaLabel" @update:model-value="set" />
  <ScalarInput v-else :spec="spec" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
</template>

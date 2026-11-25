<script>
import { defineComponent, computed } from 'vue'
import { useConfig } from '../useConfig.js'
import { hasNestedFields, itemLabelOf } from './items.js'

// Which editor a spec gets.  Everything that edits a config value goes
// through here, so a new kind of value needs one new editor component and one
// line in this function.
export function editorFor(spec, value) {
  const isObj = value != null && typeof value === 'object'
  if (spec.secret || (isObj && ('!secret' in value || '!hidden' in value))) return 'secret'
  if (spec.widget === 'load_profile') return 'profile'
  if (spec.widget?.startsWith('learning')) return 'learning'
  if (spec.widget === 'weekdays') return 'weekdays'
  if (spec.widget === 'time' || spec.widget === 'time_end') return 'time'
  if (spec.widget === 'month_day') return 'month_day'
  if (spec.widget === 'tou_periods') return 'tou_periods'
  if (spec.plugin) return 'plugin'
  if (spec.ref) return 'device'
  if (spec.tariff) return 'tariff'
  if (spec.kind === 'list') {
    if (!spec.items) return 'json'
    if (spec.items.fields) return hasNestedFields(spec.items.fields) ? 'cards' : 'table'
    return 'list'
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
export const WIDE = new Set(['table', 'cards', 'tou_periods', 'list', 'map', 'object', 'json', 'profile', 'plugin', 'learning'])

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
    // An unset device tariff means the default tariff; an unset base tariff
    // (a time-of-use tariff on top of another one) means none.
    const defaultTariff = computed(() => (props.spec.widget === 'base_tariff' ? null : cfg.defaultTariff.value))
    const set = (v) => emit('update:modelValue', v)
    const itemLabel = computed(() => itemLabelOf(props.spec, editor.value === 'table' ? 'row' : 'item'))
    return { cfg, editor, deviceOptions, tariffOptions, defaultTariff, set, itemLabel }
  },
})
</script>

<template>
  <SecretPicker v-if="editor === 'secret'" :model-value="modelValue" :suggest="name" :aria-label="ariaLabel" @update:model-value="set" />
  <LoadProfileEditor v-else-if="editor === 'profile'" :model-value="modelValue" @update:model-value="set" />
  <LearningEditor v-else-if="editor === 'learning'" :model-value="modelValue" :kind="spec.widget.split(':')[1]" @update:model-value="set" />
  <WeekdaysEditor v-else-if="editor === 'weekdays'" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <TimeInput v-else-if="editor === 'time'" :model-value="modelValue" :end="spec.widget === 'time_end'" :aria-label="ariaLabel" @update:model-value="set" />
  <MonthDayInput v-else-if="editor === 'month_day'" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <TouPeriodsEditor v-else-if="editor === 'tou_periods'" :spec="spec" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <CardListEditor v-else-if="editor === 'cards'" :spec="spec" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <PluginEditor v-else-if="editor === 'plugin'" :kind="spec.plugin" scope="device" optional :model-value="modelValue" @update:model-value="set" />
  <OptionSelect v-else-if="editor === 'device'" :options="deviceOptions" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <OptionSelect v-else-if="editor === 'tariff'" :options="tariffOptions" :default-value="defaultTariff" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <TableEditor v-else-if="editor === 'table'" :fields="spec.items.fields" :item-label="itemLabel" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <ListEditor v-else-if="editor === 'list'" :item="spec.items" :item-label="itemLabel" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <MapEditor v-else-if="editor === 'map'" :value-spec="spec.values" :key-spec="spec.keys ?? null" :item-label="itemLabel" :model-value="modelValue" @update:model-value="set" />
  <SchemaForm v-else-if="editor === 'object'" class="cfg-nested" :specs="spec.fields" :model-value="modelValue" :self-id="selfId" @update:model-value="set" />
  <JsonEditor v-else-if="editor === 'json'" :kind="spec.kind" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <EntityValueInput v-else-if="editor === 'entity-value'" :spec="spec" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
  <EntityPicker v-else-if="editor === 'entity'" :model-value="modelValue" :domains="spec.domains ?? []"
                :units="spec.units ?? []" :unit-free-domains="spec.unit_free_domains ?? []" :aria-label="ariaLabel" @update:model-value="set" />
  <ScalarInput v-else :spec="spec" :model-value="modelValue" :aria-label="ariaLabel" @update:model-value="set" />
</template>

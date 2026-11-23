<script>
import { defineComponent } from 'vue'

// Dropdown over a list of { value, label } with an empty choice (or, with
// `defaultValue`, that option marked "(default)" and shown while nothing is set); a current
// value that is not offered stays visible, marked "unknown".
export default defineComponent({
  name: 'OptionSelect',
  props: {
    options:    { type: Array, required: true },
    modelValue: { default: undefined },
    none:       { type: String, default: '— none' },
    defaultValue: { type: String, default: null },   // shown as "… (default)" while nothing is set
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],
})
</script>

<template>
  <select :value="modelValue ?? defaultValue ?? ''" :aria-label="ariaLabel"
          @change="$emit('update:modelValue', ($event.target.value && $event.target.value !== defaultValue) ? $event.target.value : undefined)">
    <option v-if="!defaultValue" value="">{{ none }}</option>
    <option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}{{ o.value === defaultValue ? ' (default)' : '' }}</option>
    <option v-if="modelValue && !options.some(o => o.value === modelValue)" :value="modelValue">{{ modelValue }} (unknown)</option>
  </select>
</template>

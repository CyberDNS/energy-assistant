import { defineComponent, computed } from 'vue'
import { useConfig } from '../useConfig.js'

// Something configured through a plugin: a `type` dropdown and, below it, the
// form for that plugin's settings.  v-model is the flat mapping written to
// config.yaml: { type, ...settings }.  Used for tariffs, forecasts and a
// forecast's fallback.
export default defineComponent({
  name: 'PluginEditor',
  props: {
    kind:       { type: String, required: true },   // 'tariff' | 'forecast'
    scope:      { type: String, default: '' },      // forecasts: 'system' | 'device'
    modelValue: { default: undefined },
    optional:   { type: Boolean, default: false },
    common:     { type: Object, default: () => ({}) },   // extra specs every entry accepts
    typeLabel:  { type: String, default: 'Type' },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const cfg = useConfig()
    const value = computed(() => (props.modelValue && typeof props.modelValue === 'object' ? props.modelValue : {}))
    const options = computed(() => cfg.plugins(props.kind, props.scope))
    const plugin = computed(() => cfg.plugins(props.kind).find(p => p.type === value.value.type) ?? null)
    const specs = computed(() => (plugin.value?.settings ? { ...plugin.value.settings, ...props.common } : null))
    const settings = computed(() => {
      const { type, ...rest } = value.value
      return rest
    })
    function setType(type) {
      if (!type) return emit('update:modelValue', undefined)
      // Keep the settings the new plugin also has.
      const next = cfg.plugins(props.kind).find(p => p.type === type)
      const allowed = next?.settings ? { ...next.settings, ...props.common } : null
      const kept = Object.fromEntries(Object.entries(settings.value).filter(([k]) => !allowed || k in allowed))
      emit('update:modelValue', { type, ...kept })
    }
    const setSettings = (s) => emit('update:modelValue', { type: value.value.type, ...(s ?? {}) })
    return { value, options, plugin, specs, settings, setType, setSettings }
  },
  template: `
    <div class="cfg-plugin">
      <div class="cfg-grid">
        <div class="cfg-field">
          <label class="cfg-label"><span class="cfg-name">{{ typeLabel }}<span v-if="!optional" class="cfg-req">*</span></span>
            <select :value="value.type ?? ''" @change="setType($event.target.value)">
              <option value="" :disabled="!optional">{{ optional ? '— none' : '— choose' }}</option>
              <option v-for="p in options" :key="p.type" :value="p.type">{{ p.title }}</option>
              <option v-if="value.type && !options.some(p => p.type === value.type)" :value="value.type">{{ value.type }} (unknown)</option>
            </select>
          </label>
          <div class="cfg-hint">{{ plugin?.description }}<template v-if="plugin?.description && value.type"> · </template><code v-if="value.type" class="cfg-key">type: {{ value.type }}</code></div>
        </div>
      </div>
      <SchemaForm v-if="specs" :specs="specs" :model-value="settings" :key-prefix="value.type + ':'" extras
                  @update:model-value="setSettings" />
      <JsonEditor v-else-if="value.type" kind="dict" :model-value="settings" @update:model-value="setSettings" />
    </div>
  `,
})

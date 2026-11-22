import { defineComponent, computed, reactive, watch } from 'vue'
import FormShell from './FormShell.js'
import { useConfig, useConfigForm, clone } from './useConfig.js'

const CONNECTIONS = {
  metered_by:    { label: 'Behind meter', description: 'Its power is part of what this meter reads. Builds the metering tree used for tariffs and costs.' },
  controlled_by: { label: 'Controlled by', description: 'The switch that executes this device\'s plan.' },
  included_in:   { label: 'Included in', description: 'A consumer whose meter already contains this load.' },
}

// Form for one device.  Every part is a SchemaForm over specs from
// /api/config/schema: the plugin's settings, the binding of each capability,
// the category's params and its connections.
export default defineComponent({
  name: 'DeviceForm',
  components: { FormShell },
  props: {
    device:   { type: Object, default: null },    // null = new device
    category: { type: String, default: null },    // preselected category for a new device
  },
  emits: ['done', 'cancel'],

  setup(props, { emit }) {
    const cfg = useConfig()
    const schema = cfg.schema
    const isNew = props.device == null
    const readOnly = !!props.device?.imported
    const form = reactive(clone(props.device) ?? {
      id: '', category: props.category ?? '', type: '', label: '', tariff: null,
      settings: {}, capabilities: {}, params: {},
      metered_by: null, controlled_by: null, included_in: null,
      forecast: null, learning: true, ha_discovery: true,
    })
    for (const key of ['settings', 'capabilities', 'params']) form[key] ??= {}

    const categorySchema = computed(() => schema.value.categories.find(c => c.category === form.category) ?? null)
    const plugins = computed(() => schema.value.plugins.filter(p => !p.categories.length || p.categories.includes(form.category)))
    const plugin = computed(() => schema.value.plugins.find(p => p.type === form.type) ?? null)

    // A new device: take the only matching plugin, drop a plugin that does not fit.
    watch(() => form.category, () => {
      if (isNew && !plugins.value.some(p => p.type === form.type)) {
        form.type = plugins.value.length === 1 ? plugins.value[0].type : ''
      }
    }, { immediate: true })

    // Basics as specs, so they render like every other field.
    const basics = computed(() => ({
      label:  { kind: 'str', label: 'Name', description: 'Shown in the UI and in Home Assistant' },
      tariff: { kind: 'str', tariff: true, label: 'Tariff', description: 'What this device\'s energy is billed on' },
    }))

    const capabilities = computed(() => {
      const cs = categorySchema.value
      if (!cs) return []
      const provides = plugin.value?.provides ?? []
      const bindable = plugin.value?.bindable ?? []
      return [...cs.required_capabilities.map(c => [c, true]), ...cs.optional_capabilities.map(c => [c, false])]
        .map(([cap, required]) => ({ cap, required, provided: provides.includes(cap), bindable: bindable.includes(cap) }))
        // Optional capabilities the plugin can neither provide nor bind are noise.
        .filter(c => c.required || c.provided || c.bindable || form.capabilities[c.cap])
    })
    // Nothing to configure when the plugin delivers everything by itself.
    const allBuiltIn = computed(() => capabilities.value.length > 0
      && capabilities.value.every(c => c.provided && !form.capabilities[c.cap]))
    // A meter that is behind no other meter is the grid connection.
    const gridConnection = computed(() => form.category === 'meter' && !form.metered_by)
    // The binding's required key only counts when the capability must be bound.
    function bindingSpecs(c) {
      return Object.fromEntries(Object.entries(plugin.value.binding).map(([k, spec]) =>
        [k, { ...spec, required: spec.required && c.required && !c.provided }]))
    }
    function setBinding(cap, value) {
      if (value === undefined) delete form.capabilities[cap]
      else form.capabilities[cap] = value
    }

    const connectionSpecs = computed(() => Object.fromEntries(
      Object.entries(categorySchema.value?.connections ?? {}).map(([conn, cats]) =>
        [conn, { kind: 'str', ref: cats, ...CONNECTIONS[conn] }])))
    const connectionValues = computed(() => Object.fromEntries(
      Object.keys(connectionSpecs.value).map(k => [k, form[k] ?? undefined])))
    function setConnections(v) {
      for (const k of Object.keys(connectionSpecs.value)) form[k] = v?.[k] ?? null
    }

    const learningKind = computed(() => schema.value.learning_kinds[form.category] ?? '')
    const showForecast = computed(() => form.category === 'consumer' || form.forecast != null)
    const showLearning = computed(() => !!learningKind.value || form.learning !== true)
    const advancedSpecs = { ha_discovery: { kind: 'bool', default: true, advanced: true, label: 'Show in Home Assistant',
      description: 'Publish this device\'s readings to Home Assistant over MQTT' } }

    const idProblem = computed(() => {
      if (!isNew || !form.id) return null
      if (!/^[a-z][a-z0-9_]*$/.test(form.id)) return 'Lower-case letters, digits and "_", starting with a letter.'
      if (cfg.devices.value.some(d => d.id === form.id)) return 'This id is already used.'
      return null
    })
    const ready = () => !readOnly && !!form.id && !!form.category && !!form.type && !idProblem.value

    const state = useConfigForm({
      kind: 'devices', id: () => form.id, value: () => form, create: isNew, ready, watchSource: form,
    })
    if (!isNew && !readOnly) state.check()

    async function save() {
      if (await state.save()) emit('done', `${form.label || form.id} ${isNew ? 'added' : 'saved'} and applied.`)
    }
    async function remove() {
      if (await state.remove()) emit('done', `${form.label || form.id} removed.`)
    }

    return { cfg, schema, isNew, readOnly, form, categorySchema, plugins, plugin, basics, capabilities,
             allBuiltIn, gridConnection,
             bindingSpecs, setBinding, connectionSpecs, connectionValues, setConnections,
             learningKind, showForecast, showLearning, advancedSpecs, idProblem, state, save, remove }
  },

  template: `
    <FormShell :title="isNew ? 'New device' : (form.label || form.id)" :form="state" :read-only="readOnly"
               :save-label="isNew ? 'Add device' : 'Save and apply'" :removable="!isNew && !readOnly"
               :remove-hint="'Remove ' + form.id + ' from config.yaml? Its history stays in the database.'"
               @save="save" @remove="remove" @cancel="$emit('cancel')">
      <template #note>
        <p v-if="readOnly" class="cfg-note">
          Imported from the Home Assistant Energy dashboard — not in config.yaml. Exclude it under
          System → Energy dashboard import, or add a device with the same sensor to replace it.
        </p>
      </template>

      <section>
        <h3>Device</h3>
        <div class="cfg-grid">
          <div class="cfg-field">
            <label class="cfg-label"><span class="cfg-name">Kind of device<span class="cfg-req">*</span></span>
              <select v-model="form.category" :disabled="!isNew">
                <option value="" disabled>— choose</option>
                <option v-for="c in schema.categories" :key="c.category" :value="c.category">{{ c.title }}</option>
              </select>
            </label>
            <div class="cfg-hint">{{ categorySchema?.description }}<template v-if="categorySchema"> · </template><code class="cfg-key">category{{ form.category ? ': ' + form.category : '' }}</code></div>
          </div>
          <div class="cfg-field">
            <label class="cfg-label"><span class="cfg-name">ID<span class="cfg-req">*</span></span>
              <input type="text" v-model.trim="form.id" :disabled="!isNew" placeholder="e.g. garage_charger"
                     autocapitalize="off" spellcheck="false" />
            </label>
            <div v-if="idProblem" class="ev-error">{{ idProblem }}</div>
            <div v-else class="cfg-hint">{{ isNew ? 'Key for stored data, MQTT and the API. Cannot be changed later.' : 'Cannot be changed: stored data is keyed by it.' }}</div>
          </div>
          <div class="cfg-field">
            <label class="cfg-label"><span class="cfg-name">Connected through<span class="cfg-req">*</span></span>
              <select v-model="form.type" :disabled="!form.category">
                <option value="" disabled>— choose</option>
                <option v-for="p in plugins" :key="p.type" :value="p.type">{{ p.title }}</option>
                <option v-if="form.type && !plugins.some(p => p.type === form.type)" :value="form.type">{{ form.type }} (not for this kind of device)</option>
              </select>
            </label>
            <div class="cfg-hint">{{ plugin?.description || 'How Energy Assistant talks to the device.' }} · <code class="cfg-key">type{{ form.type ? ': ' + form.type : '' }}</code></div>
          </div>
          <SchemaField name="label" :spec="basics.label" :model-value="form.label || undefined" @update:model-value="form.label = $event ?? ''" />
          <SchemaField name="tariff" :spec="basics.tariff" :model-value="form.tariff || undefined" @update:model-value="form.tariff = $event ?? null" />
        </div>
      </section>

      <section v-if="plugin && (!plugin.settings || Object.keys(plugin.settings).length || Object.keys(form.settings).length)">
        <h3>{{ plugin.title }}</h3>
        <SchemaForm v-if="plugin.settings" :specs="plugin.settings" :model-value="form.settings" :self-id="form.id"
                    :key-prefix="form.type + ':'" extras @update:model-value="form.settings = $event ?? {}" />
        <JsonEditor v-else kind="dict" :model-value="form.settings" @update:model-value="form.settings = $event ?? {}" />
      </section>

      <section v-if="categorySchema && plugin && capabilities.length">
        <h3>What it provides</h3>
        <p v-if="allBuiltIn" class="cfg-hint">
          Built into {{ plugin.title }}, nothing to set up:
          {{ capabilities.map(c => cfg.capabilityTitle(c.cap)).join(', ') }}.
        </p>
        <div v-else class="cfg-caps">
          <div v-for="c in capabilities" :key="c.cap" class="cfg-cap">
            <div class="cfg-cap-name">
              {{ cfg.capabilityTitle(c.cap) }}<span v-if="c.required" class="cfg-req" title="required">*</span>
              <span v-if="c.provided" class="pill pill-ok" :title="'Delivered by ' + plugin.title + ' itself'">built in</span>
              <span v-else-if="!c.bindable" class="pill pill-off">not possible with {{ plugin.title }}</span>
              <code class="cfg-key">{{ c.cap }}</code>
            </div>
            <SchemaForm v-if="c.bindable" :specs="bindingSpecs(c)" :model-value="form.capabilities[c.cap]"
                        :key-prefix="c.cap + ':'" :show-keys="false" extras @update:model-value="setBinding(c.cap, $event)" />
          </div>
        </div>
      </section>

      <section v-if="categorySchema && Object.keys(categorySchema.params).length">
        <h3>Parameters</h3>
        <p v-if="form.category === 'meter'" class="cfg-hint">{{ gridConnection
          ? 'This meter is behind no other meter, so it is the grid connection.'
          : 'A sub-meter. The grid connection limit is set on the meter that is behind no other.' }}</p>
        <SchemaForm :specs="categorySchema.params" :model-value="form.params" :self-id="form.id"
                    :context="{ metered_by: form.metered_by }"
                    :key-prefix="form.category + ':'" @update:model-value="form.params = $event ?? {}" />
      </section>

      <section v-if="Object.keys(connectionSpecs).length">
        <h3>Connections</h3>
        <SchemaForm :specs="connectionSpecs" :model-value="connectionValues" :self-id="form.id" @update:model-value="setConnections" />
      </section>

      <section v-if="showForecast || showLearning">
        <h3>Forecast and learning</h3>
        <div class="cfg-grid">
          <div v-if="showLearning" class="cfg-field cfg-wide">
            <div class="cfg-label" role="group" aria-label="learning">
              <span class="cfg-name">Learning</span>
              <LearningEditor :kind="learningKind" :model-value="form.learning" @update:model-value="form.learning = $event ?? true" />
            </div>
            <div class="cfg-hint">{{ learningKind === 'thermal_load'
              ? 'Learn how fast the temperature changes while running and while off; the configured rates are used until a model is fitted.'
              : 'Learn the consumption forecast from the device\\'s power sensor in Home Assistant.' }}</div>
          </div>
          <div v-if="showForecast" class="cfg-field cfg-wide">
            <div class="cfg-label" role="group" aria-label="forecast">
              <span class="cfg-name">Forecast</span>
              <PluginEditor kind="forecast" scope="device" optional :model-value="form.forecast ?? undefined" @update:model-value="form.forecast = $event ?? null" />
            </div>
            <div class="cfg-hint">An explicit forecast for this consumer. With learning on, a static profile here is the fallback until a model is fitted.</div>
          </div>
        </div>
      </section>

      <SchemaForm v-if="categorySchema" :specs="advancedSpecs" :model-value="{ ha_discovery: form.ha_discovery === false ? false : undefined }"
                  @update:model-value="form.ha_discovery = $event?.ha_discovery !== false" />
    </FormShell>
  `,
})

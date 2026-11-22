import { defineComponent, computed, reactive, ref } from 'vue'
import FormShell from './FormShell.js'
import { useConfig, useConfigForm, clone } from './useConfig.js'

// Form for one entry of a plugin-configured section (a tariff, a forecast).
const EntryForm = defineComponent({
  name: 'EntryForm',
  components: { FormShell },
  props: {
    collection: { type: Object, required: true },   // schema.collections[n]
    entry:      { type: Object, default: null },    // { id, type, settings } or null = new
  },
  emits: ['done', 'cancel'],
  setup(props, { emit }) {
    const cfg = useConfig()
    const isNew = props.entry == null
    const form = reactive({
      id: props.entry?.id ?? '',
      // PluginEditor edits the flat mapping written to config.yaml.
      flat: props.entry ? { type: props.entry.type, ...clone(props.entry.settings) } : {},
    })
    const singular = props.collection.plugin_kind
    const idProblem = computed(() => {
      if (!isNew || !form.id) return null
      if (!/^[a-z][a-z0-9_]*$/.test(form.id)) return 'Lower-case letters, digits and "_", starting with a letter.'
      if (cfg.file.value[props.collection.name].some(e => e.id === form.id)) return 'This id is already used.'
      return null
    })
    const value = () => {
      const { type, ...settings } = form.flat ?? {}
      return { type, settings }
    }
    const state = useConfigForm({
      kind: props.collection.name, id: () => form.id, value, create: isNew, watchSource: form,
      ready: () => !!form.id && !!form.flat?.type && !idProblem.value,
    })
    if (!isNew) state.check()
    async function save() {
      if (await state.save()) emit('done', `${singular} ${form.id} ${isNew ? 'added' : 'saved'} and applied.`)
    }
    async function remove() {
      if (await state.remove()) emit('done', `${singular} ${form.id} removed.`)
    }
    return { isNew, form, singular, idProblem, state, save, remove }
  },
  template: `
    <FormShell :title="isNew ? 'New ' + singular : form.id" :form="state" :removable="!isNew"
               :save-label="isNew ? 'Add ' + singular : 'Save and apply'"
               :remove-hint="'Remove ' + form.id + ' from config.yaml?'"
               @save="save" @remove="remove" @cancel="$emit('cancel')">
      <section>
        <div class="cfg-grid">
          <div class="cfg-field">
            <label class="cfg-label"><span class="cfg-name">ID<span class="cfg-req">*</span></span>
              <input type="text" v-model.trim="form.id" :disabled="!isNew" autocapitalize="off" spellcheck="false" />
            </label>
            <div v-if="idProblem" class="ev-error">{{ idProblem }}</div>
            <div v-else class="cfg-hint">{{ isNew ? 'Other parts of the configuration refer to it by this id.' : 'Cannot be changed.' }}</div>
          </div>
        </div>
        <PluginEditor :kind="collection.plugin_kind" :scope="collection.scope" :common="collection.common"
                      :model-value="form.flat" @update:model-value="form.flat = $event ?? {}" />
      </section>
    </FormShell>
  `,
})

// A section whose entries are configured through plugins: list + form.
export default defineComponent({
  name: 'CollectionPanel',
  components: { EntryForm },
  props: { name: { type: String, required: true } },   // 'tariffs' | 'forecasts'
  emits: ['notice'],
  setup(props, { emit }) {
    const cfg = useConfig()
    const collection = computed(() => cfg.schema.value.collections.find(c => c.name === props.name))
    const entries = computed(() => cfg.file.value[props.name])
    const editing = ref(null)   // { entry } while a form is open

    // A few telling settings for the list row.
    function facts(e) {
      const out = []
      const specs = cfg.plugins(collection.value.plugin_kind).find(p => p.type === e.type)?.settings
      for (const [k, v] of Object.entries(e.settings)) {
        if (v == null || typeof v === 'object' || k === 'default') continue
        const label = specs?.[k]?.label ?? k
        out.push(`${label.replace(/ \(.*\)$/, '')}: ${v}`)
        if (out.length === 3) break
      }
      if (props.name === 'tariffs') {
        const users = cfg.file.value.devices.filter(d => d.tariff === e.id).length
        if (users) out.push(`${users} device${users === 1 ? '' : 's'}`)
      }
      return out
    }
    function open(entry) { emit('notice', null); editing.value = { entry }; window.scrollTo({ top: 0 }) }
    function done(message) { editing.value = null; emit('notice', message) }
    return { cfg, collection, entries, editing, facts, open, done }
  },
  template: `
    <EntryForm v-if="editing" :key="editing.entry?.id ?? 'new'" :collection="collection" :entry="editing.entry"
               @done="done" @cancel="editing = null" />
    <template v-else>
      <div class="cfg-toolbar">
        <span class="cfg-hint">{{ collection.description }}</span>
        <span class="ev-actions-spacer"></span>
        <button class="btn btn-primary" @click="open(null)">+ Add {{ collection.plugin_kind }}</button>
      </div>
      <div class="panel full">
        <div class="cfg-list">
          <button v-for="e in entries" :key="e.id" type="button" class="cfg-row" @click="open(e)">
            <span class="cfg-row-main"><span class="dev-label">{{ e.id }}</span></span>
            <span class="cfg-row-meta">
              <span class="pill cfg-type">{{ cfg.pluginTitle(collection.plugin_kind, e.type) }}</span>
              <span v-if="e.settings.default" class="pill pill-ok">default</span>
              <span v-for="f in facts(e)" :key="f" class="cfg-link">{{ f }}</span>
            </span>
            <span class="cfg-row-edit">Edit ›</span>
          </button>
          <div v-if="!entries.length" class="cfg-hint" style="padding:8px 6px">Nothing configured yet.</div>
        </div>
      </div>
    </template>
  `,
})

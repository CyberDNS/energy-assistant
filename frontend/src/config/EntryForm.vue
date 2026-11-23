<script>
import { defineComponent, computed, reactive, ref } from 'vue'
import FormShell from './FormShell.vue'
import { useConfig, useConfigForm, clone } from './useConfig.js'

// Form for one entry of a plugin-configured section (a tariff, a forecast).
export default defineComponent({
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
})
</script>

<template>
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
</template>

<script>
import { defineComponent, computed, ref, watch } from 'vue'
import FormShell from './FormShell.vue'
import { useConfig, useConfigForm, clone } from './useConfig.js'

// Form for one settings section of config.yaml (backends, optimizer, …).
export default defineComponent({
  name: 'SectionForm',
  components: { FormShell },
  props: { name: { type: String, required: true } },
  emits: ['notice'],
  setup(props, { emit }) {
    const cfg = useConfig()
    const section = computed(() => cfg.schema.value.sections.find(s => s.name === props.name))
    const stored = computed(() => cfg.file.value.sections[props.name] ?? {})
    const values = ref(clone(stored.value))
    // Follow the file when this section changes underneath (another form
    // saved, reload) — not on every re-read of the file: creating a secret
    // from this form re-reads it, and must not drop what was typed so far.
    watch(() => JSON.stringify(stored.value), () => { values.value = clone(stored.value) })
    const dirty = computed(() => JSON.stringify(values.value ?? {}) !== JSON.stringify(stored.value))

    const state = useConfigForm({
      kind: 'section', id: () => props.name, value: () => values.value ?? {}, watchSource: values,
      ready: () => dirty.value,
    })
    async function save() {
      const res = await state.save()
      if (!res) return
      const restart = res.restart_required ?? []
      emit('notice', `${section.value.title} saved and applied.`
        + (restart.length ? ` Restart the add-on for ${restart.join(', ')} to take effect.` : ''))
    }
    // The add-on's own Home Assistant, used while backends.homeassistant is empty.
    const supervisorHa = computed(() => props.name === 'backends'
      && cfg.file.value.configuration?.homeassistant?.source === 'supervisor')
    return { section, values, dirty, state, save, supervisorHa }
  },
})
</script>

<template>
  <FormShell :title="section.title" :form="state" :cancelable="false" :sticky="false" :dirty="dirty" @save="save">
    <template #note>
      <p v-if="section.description" class="cfg-note">{{ section.description }}</p>
      <p v-if="supervisorHa" class="cfg-notice" data-testid="ha-supervisor">
        ✓ Connected to Home Assistant (this installation) automatically. Fill in a Home Assistant
        connection below only to use another Home Assistant.
      </p>
    </template>
    <SchemaForm :specs="section.fields" :model-value="values" extras @update:model-value="values = $event ?? {}" />
  </FormShell>
</template>

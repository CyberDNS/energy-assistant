import { defineComponent, ref } from 'vue'

// The frame every config form shares: title, the form body (default slot),
// the list of problems reported by the server, and the action bar with save
// and — for existing entries — remove with confirmation.
export default defineComponent({
  name: 'FormShell',
  props: {
    title:       { type: String, required: true },
    form:        { type: Object, required: true },   // from useConfigForm()
    saveLabel:   { type: String, default: 'Save and apply' },
    readOnly:    { type: Boolean, default: false },
    removable:   { type: Boolean, default: false },
    removeHint:  { type: String, default: '' },
    cancelable:  { type: Boolean, default: true },
    dirty:       { type: Boolean, default: true },
    sticky:      { type: Boolean, default: true },   // keep the action bar in view (single form per page)
  },
  emits: ['save', 'remove', 'cancel'],
  setup() {
    const confirming = ref(false)
    return { confirming }
  },
  template: `
    <form class="panel cfg-form" @submit.prevent="$emit('save')">
      <div class="cfg-form-head">
        <h2>{{ title }}</h2>
        <button v-if="cancelable" type="button" class="btn btn-neutral" @click="$emit('cancel')">{{ readOnly ? 'Back' : 'Cancel' }}</button>
      </div>
      <slot name="note" />
      <fieldset :disabled="readOnly || form.busy.value">
        <slot />
      </fieldset>

      <ul v-if="form.errors.value.length" class="cfg-errors" role="alert">
        <li v-for="e in form.errors.value" :key="e">{{ e }}</li>
      </ul>

      <div v-if="!readOnly" :class="['cfg-actions', { 'cfg-sticky': sticky }]">
        <button type="submit" class="btn btn-primary" :disabled="!form.canSave.value || !dirty">
          {{ form.busy.value ? 'Applying…' : saveLabel }}
        </button>
        <span v-if="form.busy.value" class="cfg-hint">Writing config.yaml and applying it…</span>
        <span v-else-if="!dirty" class="cfg-hint">No changes.</span>
        <span v-else-if="form.valid.value" class="cfg-ok">✓ valid</span>
        <span class="ev-actions-spacer"></span>
        <template v-if="removable">
          <button v-if="!confirming" type="button" class="btn btn-danger" :disabled="form.busy.value" @click="confirming = true">Remove</button>
          <template v-else>
            <span class="cfg-hint">{{ removeHint || 'Remove it from config.yaml?' }}</span>
            <button type="button" class="btn btn-danger" :disabled="form.busy.value" @click="confirming = false; $emit('remove')">Yes, remove</button>
            <button type="button" class="btn btn-neutral" :disabled="form.busy.value" @click="confirming = false">Keep</button>
          </template>
        </template>
      </div>
    </form>
  `,
})

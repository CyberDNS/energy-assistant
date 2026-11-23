<script>
import { defineComponent, ref } from 'vue'
import { useConfig } from './useConfig.js'
import { setConfigSecret, deleteConfigSecret } from '../api.js'

// secrets.yaml: names, where each is used, and setting / replacing / removing
// values.  Values are write-only — they never come back from the server.
export default defineComponent({
  name: 'SecretsPanel',
  emits: ['notice'],
  setup(_, { emit }) {
    const cfg = useConfig()
    const editing = ref(null)     // name being set, or '+new'
    const confirming = ref(null)  // name whose removal is being confirmed
    const newName = ref('')
    const value = ref('')
    const busy = ref(false)
    const error = ref(null)

    function start(name) { editing.value = name; newName.value = ''; value.value = ''; error.value = null; confirming.value = null }
    async function submit() {
      const name = editing.value === '+new' ? newName.value.trim() : editing.value
      if (!name || !value.value) { error.value = 'Name and value are needed.'; return }
      busy.value = true
      const res = await setConfigSecret(name, value.value)
      busy.value = false
      if (!res.ok) { error.value = res.errors.join(' · '); return }
      editing.value = null
      value.value = ''
      await cfg.load()
      emit('notice', `Secret ${name} saved.`)
    }
    async function remove(name) {
      busy.value = true
      const res = await deleteConfigSecret(name)
      busy.value = false
      confirming.value = null
      if (!res.ok) { error.value = res.errors.join(' · '); return }
      await cfg.load()
      emit('notice', `Secret ${name} removed.`)
    }
    return { cfg, editing, confirming, newName, value, busy, error, start, submit, remove }
  },
})
</script>

<template>
  <div>
    <div class="cfg-toolbar">
      <span class="cfg-hint">Passwords and tokens live in secrets.yaml; the configuration refers to them by name. Values can be set and replaced here, but are never shown.</span>
      <span class="ev-actions-spacer"></span>
      <button class="btn btn-primary" @click="start('+new')">+ Add secret</button>
    </div>
    <div v-if="error" class="cfg-errors" role="alert">{{ error }}</div>

    <form v-if="editing" class="panel full cfg-secret-form" @submit.prevent="submit">
      <h2>{{ editing === '+new' ? 'New secret' : 'New value for ' + editing }}</h2>
      <div class="cfg-secret-new">
        <input v-if="editing === '+new'" type="text" v-model="newName" placeholder="name, e.g. tibber_token"
               autocapitalize="off" spellcheck="false" aria-label="Secret name" />
        <input type="password" v-model="value" placeholder="value" autocomplete="new-password" aria-label="Secret value" />
        <button type="submit" class="btn btn-primary" :disabled="busy">{{ busy ? 'Saving…' : 'Save secret' }}</button>
        <button type="button" class="btn btn-neutral" :disabled="busy" @click="editing = null">Cancel</button>
      </div>
      <div class="cfg-hint">If the configuration uses this secret, it is applied again with the new value.</div>
    </form>

    <div class="panel full">
      <div class="cfg-list">
        <div v-for="s in cfg.secrets.value" :key="s.name" class="cfg-row cfg-row-static">
          <span class="cfg-row-main"><span class="dev-label">🔑 {{ s.name }}</span></span>
          <span class="cfg-row-meta">
            <span v-if="!s.defined" class="pill pill-off">missing — set a value</span>
            <span v-if="s.from_env" class="pill pill-warn" title="An environment variable of this name overrides secrets.yaml">from environment</span>
            <span v-if="!s.used_by.length" class="cfg-link">not used</span>
            <span v-for="u in s.used_by" :key="u" class="cfg-link">{{ u }}</span>
          </span>
          <span class="cfg-row-actions">
            <template v-if="confirming === s.name">
              <button class="btn btn-danger cfg-mini" :disabled="busy" @click="remove(s.name)">Yes, remove</button>
              <button class="btn btn-neutral cfg-mini" @click="confirming = null">Keep</button>
            </template>
            <template v-else>
              <button class="btn btn-neutral cfg-mini" @click="start(s.name)">{{ s.defined ? 'Change value' : 'Set value' }}</button>
              <button v-if="s.defined && !s.used_by.length" class="btn btn-danger cfg-mini" @click="confirming = s.name">Remove</button>
            </template>
          </span>
        </div>
        <div v-if="!cfg.secrets.value.length" class="cfg-hint" style="padding:8px 6px">No secrets yet.</div>
      </div>
    </div>
  </div>
</template>

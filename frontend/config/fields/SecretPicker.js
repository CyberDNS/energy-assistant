import { defineComponent, computed, ref } from 'vue'
import { useConfig } from '../useConfig.js'
import { setConfigSecret } from '../../api.js'

// A password or token.  The config only holds the *name* of an entry in
// secrets.yaml (`!secret name`); the value is typed here once, sent to the
// server and never shown again.  Pick an existing secret, create one, or
// replace the value of the selected one.
export default defineComponent({
  name: 'SecretPicker',
  props: {
    modelValue: { default: undefined },   // { '!secret': name } | { '!hidden': true } | undefined
    suggest:    { type: String, default: '' },   // name proposed for a new secret
    ariaLabel:  { type: String, default: null },
  },
  emits: ['update:modelValue'],

  setup(props, { emit }) {
    const cfg = useConfig()
    const hidden = computed(() => !!props.modelValue?.['!hidden'])
    const name = computed(() => props.modelValue?.['!secret'] ?? '')
    const known = computed(() => cfg.secrets.value.find(s => s.name === name.value) ?? null)

    const mode = ref(null)          // null | 'new' | 'replace'
    const newName = ref('')
    const newValue = ref('')
    const busy = ref(false)
    const error = ref(null)

    function choose(v) {
      if (v === '+new') { mode.value = 'new'; newName.value = props.suggest; newValue.value = ''; error.value = null; return }
      mode.value = null
      emit('update:modelValue', v ? { '!secret': v } : undefined)
    }
    async function submit() {
      const target = mode.value === 'new' ? newName.value.trim() : name.value
      if (!target || !newValue.value) { error.value = 'Name and value are needed.'; return }
      if (mode.value === 'new' && cfg.secrets.value.some(s => s.name === target && s.defined)) {
        error.value = 'A secret with this name exists — choose it from the list.'; return
      }
      busy.value = true
      const res = await setConfigSecret(target, newValue.value)
      busy.value = false
      if (!res.ok) { error.value = res.errors.join(' · '); return }
      newValue.value = ''
      await cfg.load()
      if (mode.value === 'new') emit('update:modelValue', { '!secret': target })
      mode.value = null
      error.value = null
    }
    return { cfg, hidden, name, known, mode, newName, newValue, busy, error, choose, submit }
  },

  template: `
    <div class="cfg-secret">
      <div class="cfg-secret-row">
        <span v-if="hidden" class="pill pill-warn" title="The value is written in config.yaml itself">in config.yaml</span>
        <select :value="mode === 'new' ? '+new' : name" :aria-label="ariaLabel" @change="choose($event.target.value)">
          <option value="">{{ hidden ? '— keep as it is' : '— none' }}</option>
          <option v-for="s in cfg.secrets.value" :key="s.name" :value="s.name">🔑 {{ s.name }}{{ s.defined ? '' : ' (missing)' }}</option>
          <option value="+new">+ New secret…</option>
        </select>
        <button v-if="name && known?.defined && !mode" type="button" class="btn btn-neutral cfg-mini"
                @click="mode = 'replace'; newValue = ''; error = null">Change value</button>
        <button v-else-if="name && !mode" type="button" class="btn btn-neutral cfg-mini"
                @click="mode = 'replace'; newValue = ''; error = null">Set value</button>
      </div>
      <div v-if="mode" class="cfg-secret-new">
        <input v-if="mode === 'new'" type="text" v-model="newName" placeholder="name, e.g. tibber_token"
               autocapitalize="off" spellcheck="false" aria-label="Secret name" />
        <input type="password" v-model="newValue" :placeholder="mode === 'new' ? 'value' : 'new value for ' + name"
               autocomplete="new-password" aria-label="Secret value" @keydown.enter.prevent="submit" />
        <button type="button" class="btn btn-primary cfg-mini" :disabled="busy" @click="submit">{{ busy ? 'Saving…' : 'Save secret' }}</button>
        <button type="button" class="btn btn-neutral cfg-mini" :disabled="busy" @click="mode = null">Cancel</button>
      </div>
      <div v-if="error" class="ev-error">{{ error }}</div>
      <div v-else-if="mode" class="cfg-hint">Stored in secrets.yaml. The value is never shown again.</div>
      <div v-else-if="hidden" class="cfg-hint">Choose or create a secret to move it out of config.yaml.</div>
    </div>
  `,
})

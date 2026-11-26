<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchConfigText, saveConfigText } from '../api.js'

// config.yaml as text: for what the forms do not cover, and the way out when
// the file cannot be read at all (invalid YAML).  While Nornwatt runs,
// only a valid file is accepted; while it waits for a configuration, every
// save is kept and its remaining problems are listed.
const emit = defineEmits<{ notice: [message: string | null], saved: [] }>()

const text = ref('')
const stored = ref('')
const revision = ref<string | null>(null)
const errors = ref<string[]>([])
const loadError = ref<string | null>(null)
const busy = ref(false)
const loaded = ref(false)

const dirty = computed(() => text.value !== stored.value)
// "line 4, column 3" in a YAML error: shown next to the editor.
const errorLine = computed(() => {
  for (const e of errors.value) {
    const m = /line (\d+), column (\d+)/.exec(e)
    if (m) return { line: Number(m[1]), column: Number(m[2]) }
  }
  return null
})
const lineCount = computed(() => Math.max(text.value.split('\n').length, 1))

async function load() {
  const res = await fetchConfigText()
  loaded.value = true
  if (!res.ok) { loadError.value = res.errors.join(' · '); return }
  loadError.value = null
  text.value = stored.value = res.text
  revision.value = res.revision
  errors.value = res.errors
}
onMounted(load)

const editor = ref<HTMLTextAreaElement | null>(null)
const gutter = ref<HTMLElement | null>(null)
function syncScroll() {
  if (gutter.value && editor.value) gutter.value.scrollTop = editor.value.scrollTop
}
function goToError() {
  const at = errorLine.value
  const el = editor.value
  if (!at || !el) return
  const lines = text.value.split('\n')
  const offset = lines.slice(0, at.line - 1).reduce((n, l) => n + l.length + 1, 0) + at.column - 1
  el.focus()
  el.setSelectionRange(offset, offset)
}

async function save() {
  busy.value = true
  emit('notice', null)
  const res = await saveConfigText(text.value, revision.value)
  busy.value = false
  if (!res.ok) { errors.value = res.errors; return }
  stored.value = text.value
  revision.value = res.revision
  errors.value = res.problems ?? []
  emit('saved')
  emit('notice', res.applied === false ? 'config.yaml saved.' : 'config.yaml saved and applied.')
}
</script>

<template>
  <div v-if="loadError" class="panel full ev-error">Could not read config.yaml: {{ loadError }}</div>
  <form v-else class="panel cfg-form" data-testid="config-text" @submit.prevent="save">
    <div class="cfg-form-head"><h2>config.yaml</h2></div>
    <p class="cfg-note">
      The whole configuration as text. The other settings pages edit the same file.
      <code>!secret name</code> refers to a value under Secrets.
    </p>
    <div class="text-editor">
      <div ref="gutter" class="text-gutter" aria-hidden="true">
        <span v-for="n in lineCount" :key="n" :class="{ 'text-gutter-bad': errorLine?.line === n }">{{ n }}</span>
      </div>
      <textarea
        ref="editor" v-model="text" spellcheck="false" autocapitalize="off" autocomplete="off"
        aria-label="config.yaml" :disabled="busy || !loaded" wrap="off" @scroll="syncScroll"
      />
    </div>

    <ul v-if="errors.length" class="cfg-errors" role="alert" data-testid="config-text-errors">
      <li v-for="e in errors" :key="e">{{ e }}</li>
    </ul>

    <div class="cfg-actions cfg-sticky">
      <button type="submit" class="btn btn-primary" :disabled="busy || !dirty">
        {{ busy ? 'Applying…' : 'Save and apply' }}
      </button>
      <button v-if="errorLine" type="button" class="btn btn-neutral" @click="goToError">
        Go to line {{ errorLine.line }}
      </button>
      <span v-if="!dirty && !busy" class="cfg-hint">No changes.</span>
      <span class="ev-actions-spacer" />
      <button type="button" class="btn btn-neutral" :disabled="busy || !dirty" @click="text = stored">Undo changes</button>
    </div>
  </form>
</template>

<style scoped>
.text-editor {
  display: flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  background: var(--background); height: min(65vh, 720px);
}
.text-gutter {
  margin: 0; padding: 10px 8px 10px 10px; overflow: hidden; flex: none; text-align: right; user-select: none;
  color: var(--muted-foreground); background: var(--muted); border-right: 1px solid var(--border);
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; min-width: 3.2em;
}
.text-gutter span { display: block; }
.text-gutter-bad { color: var(--danger); font-weight: 700; }
textarea {
  flex: 1; min-width: 0; margin: 0; padding: 10px; border: 0; resize: none; outline: none;
  background: transparent; color: var(--foreground); tab-size: 2;
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre; overflow: auto;
}
</style>

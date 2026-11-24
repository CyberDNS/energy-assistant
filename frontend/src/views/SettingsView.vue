<script setup lang="ts">
import { inject, onMounted, provide, ref, watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import TextPanel from '../config/TextPanel.vue'
import { provideConfig } from '../config/useConfig.js'
import { configRevision } from '../stores/configState'

// The Settings area: loads config.yaml and its schemas once and shares them
// with the category list and every category page (useConfig()).
const cfg = provideConfig()
const route = useRoute()

// The line confirming the last save ("Tariffs saved and applied.").
const notice = ref<string | null>(null)
provide('settingsNotice', notice)
watch(() => route.path, () => { notice.value = null })

onMounted(async () => {
  await cfg.load()
  cfg.loadEntities()
})
const refreshStatus = inject<() => void>('refresh', () => {})
async function onTextSaved() {
  configRevision.value++
  refreshStatus()
}
// Applied from the banner ("Apply now"): the file on screen may be stale.
watch(configRevision, () => cfg.load())
</script>

<template>
  <div class="cfg-ui">
    <template v-if="cfg.error.value">
      <div v-if="notice" class="cfg-notice" role="status">{{ notice }}</div>
      <!-- config.yaml cannot be read (e.g. invalid YAML): the forms need it,
           so fix it as text here. -->
      <div class="panel full ev-error" data-testid="config-unreadable">
        The settings pages need a config.yaml they can read. Fix it below (the problem is
        listed above); then the other settings pages are back.
      </div>
      <TextPanel @notice="notice = $event" @saved="onTextSaved" />
    </template>
    <div v-else-if="!cfg.schema.value || !cfg.file.value" class="text-muted-foreground py-6 text-sm">Loading…</div>
    <RouterView v-else />
  </div>
</template>

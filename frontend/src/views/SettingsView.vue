<script setup lang="ts">
import { onMounted, provide, ref, watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'
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
// Applied from the banner ("Apply now"): the file on screen may be stale.
watch(configRevision, () => cfg.load())
</script>

<template>
  <div class="cfg-ui">
    <div v-if="cfg.error.value" class="panel full ev-error">
      Could not load the configuration: {{ cfg.error.value }}
    </div>
    <div v-else-if="!cfg.schema.value || !cfg.file.value" class="text-muted-foreground py-6 text-sm">Loading…</div>
    <RouterView v-else />
  </div>
</template>

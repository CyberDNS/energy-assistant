<script setup lang="ts">
import { ChevronRight } from '@lucide/vue'
import { RouterLink } from 'vue-router'
import { useConfig } from '../config/useConfig.js'
import { SETTINGS_PAGES } from '../settings'

// The list of settings categories; each opens its own page.
const cfg = useConfig()

function count(id: string): string | null {
  const file = cfg.file.value
  if (!file) return null
  const n = id === 'devices' ? file.devices.length + file.imported.length
    : id === 'tariffs' ? file.tariffs.length
      : id === 'forecasts' ? file.forecasts.length
        : id === 'secrets' ? file.secrets.length : null
  return n == null ? null : String(n)
}
</script>

<template>
  <nav class="bg-card divide-y overflow-hidden rounded-xl border" aria-label="Settings categories">
    <RouterLink
      v-for="page in SETTINGS_PAGES" :key="page.id" :to="'/settings/' + page.id" :data-testid="'settings-' + page.id"
      class="hover:bg-accent/60 focus-visible:bg-accent/60 flex items-center gap-4 px-4 py-3.5 outline-none transition-colors sm:px-5"
    >
      <span class="flex size-10 shrink-0 items-center justify-center rounded-xl text-white shadow-xs" :class="page.tone">
        <component :is="page.icon" class="size-5" />
      </span>
      <span class="min-w-0 flex-1">
        <span class="block text-[0.95rem] font-semibold">{{ page.title }}</span>
        <span class="text-muted-foreground block text-sm">{{ page.description }}</span>
      </span>
      <span v-if="count(page.id)" class="bg-muted text-muted-foreground rounded-full px-2 py-0.5 text-xs font-medium tabular-nums">
        {{ count(page.id) }}
      </span>
      <ChevronRight class="text-muted-foreground size-4.5 shrink-0" />
    </RouterLink>
  </nav>
</template>

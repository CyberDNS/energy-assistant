<script setup lang="ts">
import { ChevronRight } from '@lucide/vue'
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { pageCrumbs, pageTitle } from '@/composables/usePageTitle'
import { planning } from '@/composables/useStatus.js'

const route = useRoute()
const title = computed(() => pageTitle.value ?? route.meta.title ?? '')
</script>

<template>
  <header
    class="bg-background/85 sticky top-0 z-20 flex min-h-13 items-center gap-2 border-b px-3 pt-[var(--device-inset-top)] backdrop-blur md:px-5"
  >
    <SidebarTrigger class="text-muted-foreground -ml-1 md:hidden" />
    <nav class="flex min-w-0 items-center gap-1.5" aria-label="Breadcrumb">
      <template v-for="crumb in pageCrumbs" :key="crumb.title">
        <RouterLink
          v-if="crumb.to" :to="crumb.to"
          class="text-muted-foreground hover:text-foreground hidden shrink-0 text-sm font-medium sm:inline"
        >
          {{ crumb.title }}
        </RouterLink>
        <span v-else class="text-muted-foreground hidden shrink-0 text-sm sm:inline">{{ crumb.title }}</span>
        <ChevronRight class="text-muted-foreground/60 hidden size-3.5 shrink-0 sm:inline" />
      </template>
      <h1 class="truncate text-base font-semibold tracking-tight" data-testid="page-title">{{ title }}</h1>
    </nav>
    <span class="flex-1" />
    <transition name="fade">
      <span v-if="planning" class="plan-busy" role="status">
        <span class="spinner" /> <span class="hidden sm:inline">Recalculating plan…</span><span class="sm:hidden">Planning…</span>
      </span>
    </transition>
  </header>
</template>

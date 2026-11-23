<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from './composables/usePowerFlow'
import { computed, onMounted, provide } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { useStatus } from './composables/useStatus.js'
import { embedFromSearch, isOn } from './deeplink.js'
import AppSidebar from './layout/AppSidebar.vue'
import AttentionBanner from './layout/AttentionBanner.vue'
import PageHeader from './layout/PageHeader.vue'
import StatusBar from './layout/StatusBar.vue'
import { connectHomeAssistant } from './lib/homeassistant'
import { syncRouteWithHomeAssistant } from './lib/haRouteSync'
import { startInventory } from './stores/inventory'

const { status, error, refresh } = useStatus() as { status: Ref<Status | null>, error: Ref<string | null>, refresh: () => Promise<void> }
provide('status', status)
provide('statusError', error)
provide('refresh', refresh)

// Embed mode (?embed=1): content only, for iframes in Home Assistant dashboards.
const route = useRoute()
const router = useRouter()
const embed = computed(() => embedFromSearch || isOn(route.query.embed))
provide('embed', embed)

onMounted(() => {
  if (embed.value) return
  startInventory()
  syncRouteWithHomeAssistant(router)
  connectHomeAssistant()
})
</script>

<template>
  <div v-if="embed" class="embed-root">
    <RouterView />
  </div>

  <template v-else>
    <SidebarProvider class="min-h-[calc(100svh-var(--statusbar-h))]">
      <AppSidebar />
      <SidebarInset class="min-w-0">
        <PageHeader />
        <AttentionBanner />
        <div class="mx-auto w-full max-w-[1500px] flex-1 px-4 pt-4 pb-6 md:px-6">
          <RouterView />
        </div>
      </SidebarInset>
    </SidebarProvider>
    <StatusBar />
  </template>
</template>

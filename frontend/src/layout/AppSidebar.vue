<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '@/composables/usePowerFlow'
import { PanelLeft } from '@lucide/vue'
import { computed, inject, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import BrandMark from '@/components/BrandMark.vue'
import BrandWordmark from '@/components/BrandWordmark.vue'
import HomeAssistantIcon from '@/components/HomeAssistantIcon.vue'
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel,
  SidebarHeader, SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarRail, useSidebar,
} from '@/components/ui/sidebar'
import { usePowerFlow } from '@/composables/usePowerFlow'
import { pageNav } from '@/composables/usePageTitle'
import { haState, toggleHAMenu } from '@/lib/homeassistant'
import { visibleNav } from '@/nav'
import { vehicles } from '@/stores/inventory'
import { loadVersion, versionInfo, versionUrl } from '@/stores/version'

const status = inject('status') as Ref<Status | null>
const { categories } = usePowerFlow(status)
const route = useRoute()
const { isMobile, setOpenMobile, toggleSidebar, state } = useSidebar()

const groups = computed(() => visibleNav({ categories: categories.value, vehicleCount: vehicles.value.length }))
const active = computed(() => pageNav.value ?? route.meta.nav)

loadVersion()

// On a phone the sidebar is a drawer: close it once a page is chosen.
watch(() => route.fullPath, () => { if (isMobile.value) setOpenMobile(false) })

function openHomeAssistant() {
  if (isMobile.value) setOpenMobile(false)
  toggleHAMenu()
}
</script>

<template>
  <Sidebar collapsible="icon" class="bottom-auto h-[calc(100svh-var(--statusbar-h))]">
    <SidebarHeader class="pt-[calc(var(--device-inset-top)+0.5rem)]">
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton size="lg" as-child tooltip="Nornwatt">
            <RouterLink to="/">
              <BrandMark class="!size-8 shrink-0" aria-hidden="true" />
              <BrandWordmark class="!h-[15px] !w-[107px] shrink-0" />
            </RouterLink>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarHeader>

    <SidebarContent>
      <SidebarGroup v-for="group in groups" :key="group.id">
        <SidebarGroupLabel>{{ group.title }}</SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            <SidebarMenuItem v-for="item in group.items" :key="item.id">
              <SidebarMenuButton as-child :is-active="active === item.id" :tooltip="item.title">
                <RouterLink :to="item.to" :aria-current="active === item.id ? 'page' : undefined">
                  <component :is="item.icon" />
                  <span>{{ item.title }}</span>
                </RouterLink>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    </SidebarContent>

    <SidebarFooter>
      <SidebarMenu>
        <SidebarMenuItem v-if="haState.inHomeAssistant">
          <SidebarMenuButton
            class="border-sidebar-border bg-background hover:bg-sidebar-accent border shadow-xs"
            tooltip="Home Assistant menu" data-testid="ha-menu" @click="openHomeAssistant"
          >
            <HomeAssistantIcon class="text-[#18bcf2]" />
            <span class="font-medium">Home Assistant</span>
          </SidebarMenuButton>
        </SidebarMenuItem>
        <SidebarMenuItem v-if="!isMobile">
          <SidebarMenuButton
            class="text-muted-foreground" :tooltip="state === 'expanded' ? 'Collapse sidebar' : 'Expand sidebar'"
            @click="toggleSidebar"
          >
            <PanelLeft />
            <span>Collapse</span>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
      <component
        :is="versionUrl ? 'a' : 'span'" v-if="versionInfo"
        :href="versionUrl ?? undefined" target="_blank" rel="noopener"
        class="text-muted-foreground hover:text-foreground truncate px-2 text-xs tabular-nums group-data-[collapsible=icon]:hidden"
        :title="`Nornwatt ${versionInfo.version}`" data-testid="app-version"
      >
        v{{ versionInfo.version }}
      </component>
    </SidebarFooter>
    <SidebarRail />
  </Sidebar>
</template>

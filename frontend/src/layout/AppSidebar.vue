<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '@/composables/usePowerFlow'
import { House, PanelLeft } from '@lucide/vue'
import { computed, inject, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import logo from '@/assets/logo.svg'
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel,
  SidebarHeader, SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarRail, useSidebar,
} from '@/components/ui/sidebar'
import { usePowerFlow } from '@/composables/usePowerFlow'
import { pageNav } from '@/composables/usePageTitle'
import { haState, toggleHAMenu } from '@/lib/homeassistant'
import { visibleNav } from '@/nav'
import { vehicles } from '@/stores/inventory'

const status = inject('status') as Ref<Status | null>
const { categories } = usePowerFlow(status)
const route = useRoute()
const { isMobile, setOpenMobile, toggleSidebar, state } = useSidebar()

const groups = computed(() => visibleNav({ categories: categories.value, vehicleCount: vehicles.value.length }))
const active = computed(() => pageNav.value ?? route.meta.nav)

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
          <SidebarMenuButton size="lg" as-child tooltip="Energy Assistant">
            <RouterLink to="/">
              <img :src="logo" alt="" class="size-8 shrink-0">
              <span class="truncate text-base font-semibold tracking-tight">Energy Assistant</span>
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
            <House class="text-info" />
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
    </SidebarFooter>
    <SidebarRail />
  </Sidebar>
</template>

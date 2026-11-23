<script setup lang="ts">
import type { Ref } from 'vue'
import { computed, inject, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePageTitle } from '../composables/usePageTitle'
import CollectionPanel from '../config/CollectionPanel.vue'
import DevicesPanel from '../config/DevicesPanel.vue'
import InterfacePanel from '../config/InterfacePanel.vue'
import SecretsPanel from '../config/SecretsPanel.vue'
import SectionForm from '../config/SectionForm.vue'
import { settingsPage } from '../settings'
import { configRevision } from '../stores/configState'

// One settings category.  Changes are checked, written to config.yaml and
// applied without a restart.
const route = useRoute()
const router = useRouter()
const page = computed(() => settingsPage(String(route.params.page)))
// An unknown category (old link, typo): back to the list.
watch(page, (p) => { if (!p) router.replace('/settings') }, { immediate: true })

usePageTitle(() => page.value?.title, [{ title: 'Settings', to: '/settings' }])

const notice = inject('settingsNotice') as Ref<string | null>
const refreshStatus = inject<() => void>('refresh', () => {})

function onNotice(message: string | null) {
  notice.value = message
  if (message) {
    refreshStatus()
    configRevision.value++
  }
}
</script>

<template>
  <template v-if="page">
    <div v-if="notice" class="cfg-notice" role="status">✓ {{ notice }}</div>

    <DevicesPanel v-if="page.panel === 'devices'" @notice="onNotice" />
    <CollectionPanel v-else-if="page.panel === 'collection'" :key="page.id" :name="page.id" @notice="onNotice" />
    <SecretsPanel v-else-if="page.panel === 'secrets'" @notice="onNotice" />
    <InterfacePanel v-else-if="page.panel === 'interface'" />
    <template v-else>
      <SectionForm v-for="s in page.sections" :key="s" :name="s" @notice="onNotice" />
    </template>
  </template>
</template>

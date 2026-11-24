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
import TextPanel from '../config/TextPanel.vue'
import type { Status } from '../composables/usePowerFlow'
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
const refreshStatus = inject<() => Promise<void> | void>('refresh', () => {})
const status = inject<Ref<Status | null> | null>('status', null)
// Saved, but not running yet: config.yaml still has problems (listed in the banner).
const waiting = computed(() => status?.value?.configuration?.state === 'configuration_needed')

async function onNotice(message: string | null) {
  notice.value = message
  if (message) {
    configRevision.value++
    await refreshStatus()
  }
}
const noticeText = computed(() => {
  if (!notice.value || !waiting.value) return notice.value
  return `${notice.value.replace(/ and applied\./, '.')} It takes effect once the problems listed above are fixed.`
})
</script>

<template>
  <template v-if="page">
    <div v-if="noticeText" class="cfg-notice" :class="{ 'cfg-notice-wait': waiting }" role="status">
      {{ waiting ? '' : '✓ ' }}{{ noticeText }}
    </div>

    <DevicesPanel v-if="page.panel === 'devices'" @notice="onNotice" />
    <CollectionPanel v-else-if="page.panel === 'collection'" :key="page.id" :name="page.id" @notice="onNotice" />
    <SecretsPanel v-else-if="page.panel === 'secrets'" @notice="onNotice" />
    <InterfacePanel v-else-if="page.panel === 'interface'" />
    <TextPanel v-else-if="page.panel === 'text'" @notice="onNotice" />
    <template v-else>
      <SectionForm v-for="s in page.sections" :key="s" :name="s" @notice="onNotice" />
    </template>
  </template>
</template>

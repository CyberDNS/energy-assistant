import { defineComponent, computed, ref, watch, onMounted, inject } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { provideConfig } from '../config/useConfig.js'
import DevicesPanel from '../config/DevicesPanel.js'
import CollectionPanel from '../config/CollectionPanel.js'
import SectionForm from '../config/SectionForm.js'
import SecretsPanel from '../config/SecretsPanel.js'
import { reloadConfig } from '../api.js'

const PAGES = [
  { id: 'devices',     title: 'Devices' },
  { id: 'tariffs',     title: 'Tariffs' },
  { id: 'forecasts',   title: 'Forecasts' },
  { id: 'connections', title: 'Connections', sections: ['backends'] },
  { id: 'system',      title: 'System', sections: ['optimizer', 'controller', 'energy_dashboard', 'server', 'logging'] },
  { id: 'secrets',     title: 'Secrets' },
]

// Config tab: everything in config.yaml and secrets.yaml, by topic.  Changes
// are checked, written to the file and applied without a restart.
export default defineComponent({
  name: 'ConfigView',
  components: { RouterLink, DevicesPanel, CollectionPanel, SectionForm, SecretsPanel },

  setup() {
    const refreshStatus = inject('refresh', () => {})
    const cfg = provideConfig()
    const route = useRoute()
    const page = computed(() => PAGES.find(p => p.id === route.params.page) ?? PAGES[0])
    const notice = ref(null)
    watch(() => route.params.page, () => { notice.value = null })
    const reloading = ref(false)
    const reloadErrors = ref([])

    onMounted(async () => { await cfg.load(); cfg.loadEntities() })

    function onNotice(message) {
      notice.value = message
      if (message) refreshStatus()
    }
    async function applyFile() {
      reloading.value = true
      const res = await reloadConfig()
      reloading.value = false
      reloadErrors.value = res.ok ? [] : res.errors
      if (res.ok) onNotice('config.yaml applied.')
      await cfg.load()
    }
    return { cfg, PAGES, page, notice, reloading, reloadErrors, onNotice, applyFile }
  },

  template: `
    <div class="cfg-ui">
      <div class="cfg-subnav" role="tablist" aria-label="Configuration">
        <RouterLink v-for="p in PAGES" :key="p.id" class="learn-cat" :class="{ active: p.id === page.id }"
                    :to="'/config/' + p.id" role="tab" :aria-selected="p.id === page.id" @click="notice = null">{{ p.title }}</RouterLink>
      </div>

      <div v-if="cfg.error.value" class="panel full ev-error">Could not load the configuration: {{ cfg.error.value }}</div>
      <div v-else-if="!cfg.schema.value || !cfg.file.value" class="panel full" style="color:var(--muted)">Loading…</div>
      <template v-else>
        <div v-if="notice" class="cfg-notice" role="status">✓ {{ notice }}</div>
        <div v-if="cfg.file.value.pending_reload" class="cfg-pending">
          <span>config.yaml was changed on disk and is not applied yet.</span>
          <button class="btn btn-neutral" :disabled="reloading" @click="applyFile">{{ reloading ? 'Applying…' : 'Apply now' }}</button>
        </div>
        <ul v-if="reloadErrors.length" class="cfg-errors" role="alert">
          <li v-for="e in reloadErrors" :key="e">{{ e }}</li>
        </ul>

        <DevicesPanel v-if="page.id === 'devices'" @notice="onNotice" />
        <CollectionPanel v-else-if="page.id === 'tariffs' || page.id === 'forecasts'" :key="page.id" :name="page.id" @notice="onNotice" />
        <SecretsPanel v-else-if="page.id === 'secrets'" @notice="onNotice" />
        <template v-else>
          <SectionForm v-for="s in page.sections" :key="s" :name="s" @notice="onNotice" />
        </template>
      </template>
    </div>
  `,
})

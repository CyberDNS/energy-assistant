<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '@/composables/usePowerFlow'
import { ArrowRight, CircleAlert, TriangleAlert, X } from '@lucide/vue'
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { fetchConfigState, reloadConfig } from '@/api.js'
import { Button } from '@/components/ui/button'
import { configRevision } from '@/stores/configState'

// Things that need the user's attention, on every page, with a way to fix them.
const status = inject('status') as Ref<Status | null>
const statusError = inject('statusError') as Ref<string | null>
const refresh = inject('refresh') as () => Promise<void>
const route = useRoute()

interface Notice {
  id: string
  tone: 'danger' | 'warning'
  text: string
  detail?: string
  action?: { label: string, to?: string, run?: () => void | Promise<void> }
  dismissible: boolean
}

// ── config.yaml edited on disk but not applied ──
const pendingReload = ref(false)
const applying = ref(false)
const applyErrors = ref<string[]>([])
let timer: ReturnType<typeof setInterval> | undefined

async function checkConfig() {
  try {
    pendingReload.value = !!(await fetchConfigState()).pending_reload
  } catch {
    /* an older server without the endpoint, or unreachable: nothing to report */
  }
}
async function applyConfig() {
  applying.value = true
  const res = await reloadConfig()
  applying.value = false
  applyErrors.value = res.ok ? [] : res.errors
  if (res.ok) configRevision.value++
  await checkConfig()
  refresh()
}
onMounted(() => { checkConfig(); timer = setInterval(checkConfig, 60_000) })
onUnmounted(() => clearInterval(timer))
// Saving in Settings and coming back to the tab are the moments it changes.
watch(configRevision, checkConfig)
watch(() => route.path, checkConfig)

// ── dismissed for this browser session ──
const dismissed = ref<Set<string>>(new Set())
try {
  dismissed.value = new Set(JSON.parse(sessionStorage.getItem('energy-assistant.dismissed') ?? '[]'))
} catch { /* storage blocked */ }
function dismiss(id: string) {
  dismissed.value = new Set([...dismissed.value, id])
  try { sessionStorage.setItem('energy-assistant.dismissed', JSON.stringify([...dismissed.value])) } catch { /* storage blocked */ }
}

const notices = computed<Notice[]>(() => {
  const out: Notice[] = []
  if (statusError.value) {
    out.push({
      id: 'offline', tone: 'danger', text: 'Energy Assistant is not reachable.', detail: statusError.value,
      action: { label: 'Retry', run: refresh }, dismissible: false,
    })
  }
  if (pendingReload.value) {
    out.push({
      id: 'pending-config', tone: 'warning',
      text: 'config.yaml was changed on disk and is not applied yet.',
      detail: applyErrors.value.join(' · ') || undefined,
      action: { label: applying.value ? 'Applying…' : 'Apply now', run: applyConfig }, dismissible: false,
    })
  }
  const s = status.value
  if (s?.dry_run) {
    out.push({
      id: 'dry-run', tone: 'warning',
      text: 'Dry run is on: Energy Assistant plans, but does not control any device.',
      action: { label: 'Open settings', to: '/settings/system' }, dismissible: true,
    })
  }
  // Devices handed back to their own operation by the fail-safe (not at
  // start-up, which only waits for the first plan).
  const safe = s?.failsafe
  if (safe?.released && safe.reason && safe.reason !== 'start' && !s?.dry_run) {
    const since = safe.since ? new Date(safe.since).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null
    out.push({
      id: 'failsafe:' + (safe.since ?? ''), tone: 'danger',
      text: `Energy Assistant has released all devices to their own operation: ${safe.reason_text ?? safe.reason}.`,
      detail: [since && `Since ${since}`, safe.detail, 'Control resumes by itself once this is resolved.']
        .filter(Boolean).join(' · '),
      action: { label: 'Open plan', to: '/plan' }, dismissible: true,
    })
  }
  // A charger without a car is unavailable by design.
  const down = (s?.devices ?? []).filter(d => d.available === false && d.category !== 'charger')
  if (down.length) {
    const names = down.map(d => d.label ?? d.device_id)
    out.push({
      id: 'unavailable:' + down.map(d => d.device_id).sort().join(','), tone: 'warning',
      text: down.length === 1
        ? `${names[0]} is not responding.`
        : `${down.length} devices are not responding: ${names.join(', ')}.`,
      action: { label: 'Check connections', to: '/settings/connections' }, dismissible: true,
    })
  }
  return out.filter(n => !dismissed.value.has(n.id))
})
</script>

<template>
  <div v-if="notices.length" class="mx-auto flex w-full max-w-[1500px] flex-col gap-2 px-4 pt-4 md:px-6" data-testid="attention">
    <div
      v-for="n in notices" :key="n.id" role="alert" :data-notice="n.id.split(':')[0]"
      class="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border px-3.5 py-2.5 text-sm"
      :class="n.tone === 'danger' ? 'border-danger/40 bg-danger-soft text-danger' : 'border-warning/40 bg-warning-soft text-warning'"
    >
      <component :is="n.tone === 'danger' ? CircleAlert : TriangleAlert" class="size-4.5 shrink-0" />
      <span class="min-w-0 flex-1 basis-60">
        <span class="font-medium">{{ n.text }}</span>
        <span v-if="n.detail" class="mt-0.5 block text-xs opacity-80 [overflow-wrap:anywhere]">{{ n.detail }}</span>
      </span>
      <template v-if="n.action">
        <Button
          v-if="n.action.to" as-child size="sm"
          :class="n.tone === 'danger' ? 'bg-danger hover:bg-danger/90 text-white' : 'bg-warning hover:bg-warning/90 text-background'"
        >
          <RouterLink :to="n.action.to">{{ n.action.label }} <ArrowRight class="size-3.5" /></RouterLink>
        </Button>
        <Button
          v-else size="sm" :disabled="applying"
          :class="n.tone === 'danger' ? 'bg-danger hover:bg-danger/90 text-white' : 'bg-warning hover:bg-warning/90 text-background'"
          @click="n.action.run?.()"
        >
          {{ n.action.label }}
        </Button>
      </template>
      <button
        v-if="n.dismissible" type="button" class="rounded p-1 opacity-70 hover:opacity-100" aria-label="Dismiss"
        @click="dismiss(n.id)"
      >
        <X class="size-4" />
      </button>
    </div>
  </div>
</template>

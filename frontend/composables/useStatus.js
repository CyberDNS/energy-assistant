import { ref, onMounted, onUnmounted } from 'vue'
import { fetchStatus, streamUrl } from '../api.js'

// Bumped whenever the server publishes a new plan (SSE 'plan' event).
// Module-level so every component sees the same signal — watch it to
// refetch plan data the moment the optimizer finishes.
export const planVersion = ref(0)

// Polling fallback interval — only active while the SSE stream is down
// (the EventSource reconnects automatically).
const FALLBACK_POLL_MS = 30_000

export function useStatus() {
  const status  = ref(null)
  const loading = ref(true)
  const error   = ref(null)
  let timer = null
  let es = null
  let streamAlive = false

  async function refresh() {
    try {
      status.value  = await fetchStatus()
      error.value   = null
    } catch (e) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  }

  onMounted(() => {
    refresh()  // immediate first paint

    // Live updates via Server-Sent Events: the server pushes a fresh status
    // snapshot every ~3 s and a 'plan' event when a new plan is published.
    // Vue reactivity propagates the new data — no component refresh needed.
    try {
      es = new EventSource(streamUrl)
      es.addEventListener('status', (e) => {
        streamAlive   = true
        status.value  = JSON.parse(e.data)
        error.value   = null
        loading.value = false
      })
      es.addEventListener('plan', () => { planVersion.value++ })
      es.onerror = () => { streamAlive = false }  // EventSource auto-reconnects
    } catch {
      es = null
    }

    // Safety net: slow polling that only does work while the stream is down.
    timer = setInterval(() => { if (!streamAlive) refresh() }, FALLBACK_POLL_MS)
  })

  onUnmounted(() => {
    clearInterval(timer)
    es?.close()
  })

  return { status, loading, error, refresh }
}

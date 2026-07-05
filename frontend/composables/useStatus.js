import { ref, onMounted, onUnmounted } from 'vue'
import { fetchStatus } from '/ui/api.js'

export function useStatus() {
  const status  = ref(null)
  const loading = ref(true)
  const error   = ref(null)
  let timer = null

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
    refresh()
    timer = setInterval(refresh, 30_000)
  })

  onUnmounted(() => clearInterval(timer))

  return { status, loading, error, refresh }
}

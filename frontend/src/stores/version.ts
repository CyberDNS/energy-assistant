// Version of the running build, fetched once (it only changes with a restart,
// which reloads the page anyway).
import { computed, ref } from 'vue'
import { fetchVersion } from '../api.js'

export interface VersionInfo {
  /** As shown to users: "0.1.3" or "0.1.3-dev+1a2b3c4". */
  version: string
  release: string
  channel: string | null
  commit: string | null
}

const REPO = 'https://github.com/CyberDNS/energy-assistant'

export const versionInfo = ref<VersionInfo | null>(null)

/** Where the build's changes can be read: its commit for dev builds, its release otherwise. */
export const versionUrl = computed(() => {
  const v = versionInfo.value
  if (!v) return null
  if (v.channel && v.commit) return `${REPO}/commit/${v.commit}`
  if (!v.channel) return `${REPO}/releases/tag/v${v.release}`
  return null
})

let started = false

export function loadVersion(): void {
  if (started) return
  started = true
  fetchVersion().then((v: VersionInfo) => { versionInfo.value = v }).catch(() => { started = false })
}

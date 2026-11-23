// The title and breadcrumb of the current page, shown in the header.
// Routes give a static title (meta.title); pages whose title depends on data
// (a car, a settings category) set it with usePageTitle().
import type { MaybeRefOrGetter } from 'vue'
import { onUnmounted, ref, toValue, watchEffect } from 'vue'

export interface Crumb {
  title: string
  to?: string
}

export const pageTitle = ref<string | null>(null)
export const pageCrumbs = ref<Crumb[]>([])

export function usePageTitle(title: MaybeRefOrGetter<string | null | undefined>, crumbs: MaybeRefOrGetter<Crumb[]> = []): void {
  watchEffect(() => {
    pageTitle.value = toValue(title) ?? null
    pageCrumbs.value = toValue(crumbs)
  })
  onUnmounted(() => {
    pageTitle.value = null
    pageCrumbs.value = []
  })
}

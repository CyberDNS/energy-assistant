// Keeps Home Assistant's URL and the app's page in step while the app runs in
// HA's app panel, so that a link into the app (…/<app>/plan) opens that page
// and a reload of Home Assistant comes back to the page that was open.
//
// HA tells us the part of its URL behind the app's own path (`route.path`);
// we answer page changes with `home-assistant/navigate` and `replace`, which
// rewrites HA's URL without adding a history entry: the back button keeps
// walking through the pages of the app, as it does without Home Assistant.
import type { Router } from 'vue-router'
import type { HARoute } from './homeassistant'
import { haState, navigateInHA, onHARoute } from './homeassistant'

const pageOf = (path: string | undefined) => (!path || path === '/' ? '/' : path.replace(/\/+$/, ''))

export function syncRouteWithHomeAssistant(router: Router): void {
  let first = true

  function mirror(path: string): void {
    const route = haState.route
    if (!route || pageOf(route.path) === path) return
    navigateInHA(route.prefix + (path === '/' ? '' : path), { replace: true })
  }

  onHARoute((route: HARoute) => {
    const wanted = pageOf(route.path)
    const current = router.currentRoute.value.path
    if (first) {
      first = false
      // HA's URL names a page: open it. Otherwise keep ours and tell HA.
      if (wanted !== '/' && wanted !== current) router.replace(wanted)
      else mirror(current)
      return
    }
    // Later changes come from HA's own history (back, forward) or a link in HA.
    if (wanted !== current) router.replace(wanted)
  })

  router.afterEach((to, from) => {
    if (to.path !== from.path) mirror(to.path)
  })
}

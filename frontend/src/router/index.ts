import type { RouteRecordRaw } from 'vue-router'
import { createRouter, createWebHashHistory } from 'vue-router'
import { embedFromSearch, isOn, rememberPage } from '../deeplink.js'

declare module 'vue-router' {
  interface RouteMeta {
    /** Page title in the header; pages with a dynamic title set it themselves (usePageTitle). */
    title?: string
    /** The sidebar item (nav.ts) this page belongs to. */
    nav?: string
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/', component: () => import('../views/LiveView.vue'), meta: { title: 'Home', nav: 'home' } },
  { path: '/plan', component: () => import('../views/PlanView.vue'), meta: { title: 'Plan', nav: 'plan' } },
  { path: '/cars', component: () => import('../views/CarsView.vue'), meta: { title: 'Cars', nav: 'cars' } },
  { path: '/cars/:vehicle', component: () => import('../views/CarView.vue'), meta: { nav: 'cars' } },
  { path: '/batteries', component: () => import('../views/BatteriesView.vue'), meta: { title: 'Batteries', nav: 'batteries' } },
  { path: '/loads', component: () => import('../views/LoadsView.vue'), meta: { title: 'Heating & loads', nav: 'loads' } },
  { path: '/savings', component: () => import('../views/SavingsView.vue'), meta: { title: 'Savings', nav: 'savings' } },
  { path: '/history', component: () => import('../views/HistoryView.vue'), meta: { title: 'History', nav: 'history' } },
  { path: '/learning', component: () => import('../views/LearningView.vue'), meta: { title: 'Learning', nav: 'learning' } },
  {
    path: '/settings',
    component: () => import('../views/SettingsView.vue'),
    meta: { nav: 'settings' },
    children: [
      { path: '', component: () => import('../views/SettingsIndex.vue'), meta: { title: 'Settings' } },
      { path: ':page', component: () => import('../views/SettingsPageView.vue') },
    ],
  },

  // Links from before the sidebar (bookmarks, iframes in Home Assistant dashboards).
  { path: '/plan/:vehicle', redirect: to => ({ path: `/cars/${encodeURIComponent(String(to.params.vehicle))}`, query: to.query }) },
  { path: '/config', redirect: '/settings' },
  { path: '/config/:page', redirect: to => ({ path: `/settings/${String(to.params.page)}`, query: to.query }) },
  { path: '/devices', redirect: '/settings/devices' },
  { path: '/:rest(.*)*', redirect: '/' },
]

// A factory: the history reads the URL when it is created, which has to happen
// after applyInitialRoute() (main.ts).
export function createAppRouter() {
  const router = createRouter({
    history: createWebHashHistory(),
    routes,
    scrollBehavior(to, from, saved) {
      if (saved) return saved
      return to.path === from.path ? undefined : { top: 0 }
    },
  })

  router.beforeEach((to) => {
    // #/plan?vehicle=<vehicle> used to show one car.
    if (to.path === '/plan' && to.query.vehicle) {
      const { vehicle, ...query } = to.query
      return { path: `/cars/${encodeURIComponent(String(vehicle))}`, query }
    }
    return true
  })

  // Remember the page used last for hosts that cannot deep-link (see deeplink.js).
  // Embedded views are one-off windows into the app and never change it.
  router.afterEach((to) => {
    if (!embedFromSearch && !isOn(to.query.embed)) rememberPage(to.path)
  })

  return router
}

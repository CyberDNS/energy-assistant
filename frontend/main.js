import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.js'
import LiveView from './views/LiveView.js'
import PlanView from './views/PlanView.js'
import { applyInitialRoute, embedFromSearch, isOn, rememberTab } from './deeplink.js'

applyInitialRoute()

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/',     component: LiveView },
    { path: '/plan', component: PlanView },
    { path: '/plan/:vehicle', component: PlanView },
  ],
})

// Remember the last-used tab for hosts that cannot deep-link (see deeplink.js).
// Embedded views are one-off windows into the app and never change it.
router.afterEach((to) => {
  if (!embedFromSearch && !isOn(to.query.embed)) rememberTab(to.path)
})

const app = createApp(App)
app.use(router)

app.mount('#app')

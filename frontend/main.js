import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.js'
import LiveView from './views/LiveView.js'
import PlanView from './views/PlanView.js'
import LearningView from './views/LearningView.js'
import SavingsView from './views/SavingsView.js'
import ConfigView from './views/ConfigView.js'
import { registerFieldComponents } from './config/fields/index.js'
import { applyInitialRoute, embedFromSearch, isOn, rememberTab } from './deeplink.js'

applyInitialRoute()

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/',     component: LiveView },
    { path: '/plan', component: PlanView },
    { path: '/plan/:vehicle', component: PlanView },
    { path: '/learning', component: LearningView },
    { path: '/savings', component: SavingsView },
    { path: '/config/:page?', component: ConfigView },
    { path: '/devices', redirect: '/config/devices' },
  ],
})

// Remember the last-used tab for hosts that cannot deep-link (see deeplink.js).
// Embedded views are one-off windows into the app and never change it.
router.afterEach((to) => {
  if (!embedFromSearch && !isOn(to.query.embed)) rememberTab(to.path.startsWith('/config') ? '/config' : to.path)
})

const app = createApp(App)
registerFieldComponents(app)
app.use(router)

app.mount('#app')

import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from '/ui/App.js'
import LiveView from '/ui/views/LiveView.js'
import PlanView from '/ui/views/PlanView.js'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/',     component: LiveView },
    { path: '/plan', component: PlanView },
  ],
})

const app = createApp(App)
app.use(router)

app.mount('#app')

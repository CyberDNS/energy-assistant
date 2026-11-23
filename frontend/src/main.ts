import './styles/main.css'
import { createApp } from 'vue'
import App from './App.vue'
import { registerFieldComponents } from './config/fields/index.js'
import { applyInitialRoute, embedFromSearch } from './deeplink.js'
import { createAppRouter } from './router'

// Before the router reads the URL: ?view= / ?vehicle= and the page used last.
applyInitialRoute()
if (embedFromSearch) document.documentElement.classList.add('embed')

const app = createApp(App)
registerFieldComponents(app)
app.use(createAppRouter())
app.mount('#app')

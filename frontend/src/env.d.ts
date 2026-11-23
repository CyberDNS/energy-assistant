/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}

declare module 'plotly.js-basic-dist-min'

declare module '*.svg' {
  const url: string
  export default url
}

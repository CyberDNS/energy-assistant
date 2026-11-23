import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// The app is served at "/" directly and under a changing path prefix behind
// Home Assistant ingress, so every URL in the build has to be relative.
export default defineConfig({
  base: './',
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    // Into the Python package: the server serves it from there, in a dev
    // checkout and in the wheel alike (see server/__init__.py).
    outDir: '../src/energy_assistant/frontend',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    // `npm run dev` against a running Energy Assistant (default: local server).
    proxy: {
      '/api': { target: process.env.EA_API ?? 'http://localhost:8088', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
})

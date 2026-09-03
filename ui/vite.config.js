import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      // CrabDeck Gateway (WebSocket agent bus)
      '/ws': {
        target:       'ws://localhost:8765',
        ws:           true,
        changeOrigin: true,
        rewrite:      path => path.replace(/^\/ws/, '')
      },
      // Orchestrator Core REST API (FastAPI)
      '/api': {
        target:       'http://localhost:8000',
        changeOrigin: true,
        rewrite:      path => path.replace(/^\/api/, '')
      },
      // Shell Cracked memory vault
      '/vault': {
        target:       'http://localhost:7070',
        changeOrigin: true,
        rewrite:      path => path.replace(/^\/vault/, '')
      },
      // Gateway HTTP health/metrics
      '/gw': {
        target:       'http://localhost:8765',
        changeOrigin: true,
        rewrite:      path => path.replace(/^\/gw/, '')
      }
    }
  }
})

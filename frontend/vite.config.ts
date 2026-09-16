import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The backend runs as a separate process. Proxying /api and /ws in dev means the
// frontend never needs CORS or credentials of its own.
const BACKEND = process.env['VITE_BACKEND_URL'] ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, 'src') },
  },
  // Built into the backend package, which serves it: `uvx alpha-harness` needs no Node.
  build: {
    outDir: path.resolve(import.meta.dirname, '../backend/src/alpha_harness/web'),
    emptyOutDir: true,
    // Screens are already lazy. Splitting the framework out of the entry as well cost 24 kB
    // gzipped more on first load for chunks that cache across wheel upgrades — worth it
    // over a network, worth nothing from 127.0.0.1. The warning is written for a CDN.
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
      '/openapi.json': { target: BACKEND, changeOrigin: true },
    },
  },
})

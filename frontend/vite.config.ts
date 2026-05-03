import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'url'

const BACKEND_URL = 'http://localhost:8000'

// SPA-friendly bypass: return index.html for browser navigation
const spaBypass = (req: { headers: { accept?: string } }) => {
  const accept = req.headers.accept ?? ''
  if (accept.includes('text/html') && !accept.includes('application/json')) {
    return '/index.html'
  }
}

const createSpaProxy = () => ({
  target: BACKEND_URL,
  bypass: spaBypass,
})

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
  },
  server: {
    proxy: {
      '/api': BACKEND_URL,
      '/analyze': BACKEND_URL,
      '/re-analyze': BACKEND_URL,
      '/results': createSpaProxy(),
      '/history': createSpaProxy(),
      '/status': createSpaProxy(),
      '/health': BACKEND_URL,
      '/ai-configs': BACKEND_URL,
    },
  },
})

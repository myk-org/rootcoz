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
  cacheDir: process.env.VITE_CACHE_DIR || 'node_modules/.vite',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    maxWorkers: 4,
    setupFiles: './src/test/setup.ts',
    css: true,
    // 15 s, not vitest's 5 s default. Measured with the suite pinned to two
    // cores plus two burners on those cores — the same recipe that reproduced
    // the CI timeout on UsersPage.test.tsx — several jsdom tests need more than
    // 5 s of wall time on a loaded runner: UsersPage pagination 4321-6331 ms,
    // AnalysisAiPicker 5082-7133 ms, UsersGrant 4151-5137 ms. With 5 s the
    // suite failed on those three; with 15 s it passed 3/3 runs and the worst
    // test in the whole suite took 7280 ms (2x headroom). A budget that fails
    // on the clock rather than an assertion teaches people to weaken tests.
    testTimeout: 15_000,
  },
  server: {
    // Polling required for Docker bind mount — inotify doesn't work across container boundaries
    watch: {
      usePolling: true,
      interval: 2000,
    },
    proxy: {
      '/api': BACKEND_URL,
      '/analyze': BACKEND_URL,
      '/re-analyze': BACKEND_URL,
      '/results': createSpaProxy(),
      '/history': createSpaProxy(),
      '/status': createSpaProxy(),
      '/health': BACKEND_URL,
    },
  },
})

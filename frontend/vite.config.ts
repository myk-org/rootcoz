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
    // 15 s, not vitest's 5 s default: at 5 s a full-suite run pinned to two cores
    // plus two burners (the recipe that reproduces the CI flake) fails with
    // "Test timed out in 5000ms" and no assertion error, on whichever jsdom tests
    // are slowest that run — UsersPage pagination and its search-debounce test,
    // AnalysisAiPicker's unavailable-sources test, UsersGrant's grant tests. The
    // worst measured 6.2-8.1 s over loaded runs, so 15 s is ~2x headroom.
    //
    // The cost is jsdom rendering, not the interaction API: the pagination test
    // costs the same with fireEvent.click as with userEvent.click (~1.9 s on two
    // loaded cores, 3/3 runs each), so it keeps userEvent and its real pointer
    // sequence. Per-test budgets would flake again on the next slow test, and
    // they cannot mask an assertion: find/waitFor keep their 1 s window, so a
    // wrong expectation still fails in 1 s, not 15.
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

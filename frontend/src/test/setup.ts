import { JSDOM } from 'jsdom'
import '@testing-library/jest-dom'

// Node >= 22 ships experimental Web Storage globals that are inert unless the
// process is started with --localstorage-file. Vitest's global sync copies
// that broken value over jsdom's window storage, leaving localStorage and
// sessionStorage undefined in tests. Rebind both to a working implementation.
function ensureWebStorage(key: 'localStorage' | 'sessionStorage') {
  const existing = Object.getOwnPropertyDescriptor(globalThis, key)?.value
  if (existing) return
  const storage = new JSDOM('', { url: 'http://localhost/' }).window[key]
  Object.defineProperty(globalThis, key, {
    value: storage,
    configurable: true,
    writable: true,
  })
  if (typeof window !== 'undefined') {
    Object.defineProperty(window, key, {
      value: storage,
      configurable: true,
      writable: true,
    })
  }
}

ensureWebStorage('localStorage')
ensureWebStorage('sessionStorage')

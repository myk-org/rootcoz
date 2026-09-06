import '@testing-library/jest-dom'

// Node >= 22 ships experimental Web Storage globals that are inert unless the
// process is started with --localstorage-file. Vitest's global sync copies
// that broken value over jsdom's window storage, leaving localStorage and
// sessionStorage undefined in tests. Rebind both to a working implementation.
class MemoryStorage implements Storage {
  private readonly store = new Map<string, string>()

  get length(): number {
    return this.store.size
  }

  clear(): void {
    this.store.clear()
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null
  }

  key(index: number): string | null {
    return [...this.store.keys()][index] ?? null
  }

  removeItem(key: string): void {
    this.store.delete(key)
  }

  setItem(key: string, value: string): void {
    this.store.set(String(key), String(value))
  }
}

function ensureWebStorage(key: 'localStorage' | 'sessionStorage') {
  const existing = Object.getOwnPropertyDescriptor(globalThis, key)?.value
  if (existing) return
  const storage = new MemoryStorage()
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

import { normalizeProvider } from '@/lib/aiProviders'

/** A user-chosen AI pair. An empty provider+model means "the server default". */
export type AiPair = { provider: string; model: string }

/** A remembered pair tagged with the username it was read for. */
export type AiPairEntry = { owner: string; pair: AiPair | null }

const KEY_PREFIX = 'rootcoz_last_ai_pair'

/**
 * Memoised snapshots, so readLastAiPair returns the SAME object while storage is
 * unchanged — a fresh parse per call makes useSyncExternalStore re-render forever.
 */
const cache = new Map<string, { key: string; raw: string | null; value: AiPairEntry | null }>()
const listeners = new Map<string, Set<() => void>>()

function storageKey(username: string): string {
  return `${KEY_PREFIX}:${username}`
}

function readRaw(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null // storage unavailable (private mode, quota) reads as nothing remembered
  }
}

function parseEntry(raw: string | null, owner: string): AiPairEntry | null {
  if (raw === null) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const { provider, model } = parsed as Partial<AiPair>
    if (typeof provider !== 'string' || typeof model !== 'string') return null
    return { owner, pair: { provider, model } }
  } catch {
    return null
  }
}

function notify(username: string): void {
  for (const listener of [...(listeners.get(username) ?? [])]) listener()
}

function samePair(a: AiPair | null | undefined, b: AiPair | null): boolean {
  if (!a || !b) return !a && !b
  return a.provider === b.provider && a.model === b.model
}

// Storage can be unavailable (private mode, blocked cookies, quota). Persistence is
// the only thing lost then, so say it once instead of on every pick.
let persistenceWarned = false

// Another tab changed (or cleared) storage: drop the snapshot so the next read
// re-parses, then wake every mounted picker for that user.
window.addEventListener('storage', (event) => {
  if (event.key === null) {
    cache.clear()
    for (const username of [...listeners.keys()]) notify(username)
    return
  }
  const prefix = `${KEY_PREFIX}:`
  if (!event.key.startsWith(prefix)) return
  const username = event.key.slice(prefix.length)
  cache.delete(username)
  notify(username)
})

/**
 * The pair remembered for `username`, or null when nothing is remembered (no key,
 * corrupt value, empty username). The returned entry keeps the username it was read
 * for, so a caller never writes it under another account.
 */
export function readLastAiPair(username: string): AiPairEntry | null {
  if (!username) return null
  const key = storageKey(username)
  const raw = readRaw(key)
  const cached = cache.get(username)
  if (cached && cached.key === key && cached.raw === raw) return cached.value
  const value = parseEntry(raw, username)
  cache.set(username, { key, raw, value })
  return value
}

/**
 * Remember `pair` for `username`, or remove the entry with `null`. Only a complete
 * pair or the explicit-clear sentinel `{provider: '', model: ''}` is stored: a half
 * pair is a mid-selection state, so it leaves the remembered pair untouched.
 */
export function writeLastAiPair(username: string, pair: AiPair | null): void {
  if (!username) return
  if (pair && !pair.provider !== !pair.model) return
  const key = storageKey(username)
  const normalized = pair ? { provider: normalizeProvider(pair.provider), model: pair.model } : null
  const raw = normalized ? JSON.stringify(normalized) : null
  const cached = cache.get(username)
  if (cached && cached.key === key && cached.raw === raw && readRaw(key) === raw && samePair(cached.value?.pair, normalized)) return
  // The in-memory value comes first and stays authoritative: a pick must not snap
  // back just because storage refused the write. `raw` is what storage holds right
  // now, so React's synchronous snapshot check during notify returns this value.
  const value: AiPairEntry | null = normalized ? { owner: username, pair: normalized } : null
  const observed = readRaw(key)
  cache.set(username, { key, raw: observed, value })
  notify(username)
  try {
    if (raw === null) localStorage.removeItem(key)
    else localStorage.setItem(key, raw)
    cache.set(username, { key, raw, value })
  } catch {
    if (!persistenceWarned) {
      persistenceWarned = true
      console.debug('[ai-pair] storage unavailable, keeping the pick in memory only')
    }
    // The cache already holds the value with the storage state it still observes.
  }
}

/** Subscribe to writes for `username` — how simultaneously mounted surfaces agree. */
export function subscribeLastAiPair(username: string, onChange: () => void): () => void {
  if (!username) return () => {}
  const set = listeners.get(username) ?? new Set<() => void>()
  set.add(onChange)
  listeners.set(username, set)
  return () => {
    set.delete(onChange)
    if (set.size === 0) listeners.delete(username)
  }
}

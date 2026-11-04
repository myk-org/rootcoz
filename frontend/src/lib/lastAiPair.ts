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
  if (readRaw(key) === raw) return
  try {
    if (raw === null) localStorage.removeItem(key)
    else localStorage.setItem(key, raw)
  } catch {
    // Storage unavailable: the pair simply is not remembered.
  }
  // Drop the memoised snapshot so the next read re-parses and every mounted
  // surface re-renders with the new value.
  cache.delete(username)
  notify(username)
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

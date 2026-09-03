import { useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import type { AiModelsResponse, ProviderStatus } from '@/types'
import { buildProviderOptions, type AiProviderOption } from '@/lib/aiProviders'

export type { ProviderStatus }

type CatalogState = {
  providerKeys: string[]
  /** @deprecated Use providerKeys. */
  enabled: string[]
  providerStatus: Record<string, ProviderStatus>
}

const EMPTY_CATALOG: CatalogState = { providerKeys: [], enabled: [], providerStatus: {} }

/** Shared in-flight / completed catalog so concurrent hook mounts share one fetch. */
let catalogInflight: Promise<CatalogState> | null = null
let catalogCache: CatalogState | null = null
/** Auth-scoped key (`username:adminFlag`) for the active cache entry. */
let catalogCacheKey: string | null = null
/** Mounted useProviderCatalog consumers — notified on cache reset. */
const catalogSubscribers = new Set<() => void>()

function catalogKeyFor(username: string, isAdmin: boolean, authenticated: boolean): string {
  if (!authenticated || !username) return 'anon'
  return `${username}:${isAdmin ? '1' : '0'}`
}

function loadProviderCatalog(cacheKey: string): Promise<CatalogState> {
  if (catalogCache && catalogCacheKey === cacheKey) {
    return Promise.resolve(catalogCache)
  }
  if (catalogInflight && catalogCacheKey === cacheKey) {
    return catalogInflight
  }
  catalogCacheKey = cacheKey
  catalogCache = null
  const req = api
    .get<AiModelsResponse>('/api/ai-models')
    .then((res) => {
      const providers = res.providers ?? {}
      const providerKeys = Object.keys(providers)
      const next: CatalogState = {
        providerKeys,
        enabled: providerKeys.filter((p) => (providers[p] ?? []).length > 0),
        providerStatus: res.provider_status ?? {},
      }
      // Only commit if this response still matches the active auth scope.
      if (catalogCacheKey === cacheKey) {
        catalogCache = next
      }
      // Don't clear a newer inflight started after this request.
      if (catalogInflight === req) {
        catalogInflight = null
      }
      return next
    })
    .catch((err) => {
      if (catalogInflight === req) {
        catalogInflight = null
      }
      throw err
    })
  catalogInflight = req
  return req
}

/**
 * Clear shared catalog cache and notify mounted useProviderCatalog consumers
 * so they refetch (login/logout/admin refresh/tests).
 */
export function resetProviderCatalogCache(): void {
  catalogInflight = null
  catalogCache = null
  catalogCacheKey = null
  // Wake every mounted hook — cacheKey alone does not change on refresh.
  for (const notify of [...catalogSubscribers]) {
    notify()
  }
}

/** @deprecated Prefer resetProviderCatalogCache — kept for existing tests. */
export function _resetProviderCatalogCacheForTests(): void {
  resetProviderCatalogCache()
}

/** Provider ids that currently have at least one discovered model. */
export function useEnabledProviders(): string[] {
  const { enabled } = useProviderCatalog()
  return enabled
}

/** Models catalog + provider_status (e.g. cursor auth). */
export function useProviderCatalog(): {
  providerKeys: string[]
  /** @deprecated Use providerKeys. */
  enabled: string[]
  providerStatus: Record<string, ProviderStatus>
} {
  const { username, isAdmin, authenticated } = useAuth()
  const cacheKey = catalogKeyFor(username, isAdmin, authenticated)
  const [reloadToken, setReloadToken] = useState(0)
  const [state, setState] = useState<CatalogState>(
    catalogCache && catalogCacheKey === cacheKey ? catalogCache : EMPTY_CATALOG,
  )

  useEffect(() => {
    const notify = () => setReloadToken((n) => n + 1)
    catalogSubscribers.add(notify)
    return () => {
      catalogSubscribers.delete(notify)
    }
  }, [])

  useEffect(() => {
    let ignore = false
    loadProviderCatalog(cacheKey)
      .then((next) => {
        if (!ignore) setState(next)
      })
      .catch(() => {
        if (!ignore) setState(EMPTY_CATALOG)
      })
    return () => {
      ignore = true
    }
  }, [cacheKey, reloadToken])

  return state
}

/**
 * Provider dropdown options: providers with models, plus current selection,
 * plus providers with a non-ok status (e.g. Cursor auth expired — keep visible).
 */
export function useProviderOptions(
  currentValues: string | string[] | undefined = undefined,
): AiProviderOption[] {
  const { providerKeys, providerStatus } = useProviderCatalog()
  const currentKey = Array.isArray(currentValues)
    ? currentValues.join('\0')
    : (currentValues ?? '')

  return useMemo(() => {
    const current = currentKey ? currentKey.split('\0') : []
    const knownProviders = new Set([...providerKeys, ...current])
    const keepVisible = Object.entries(providerStatus)
      .filter(([id, st]) => st && st.ok === false && knownProviders.has(id))
      .map(([id]) => id)
    return buildProviderOptions(providerKeys, [...current, ...keepVisible])
  }, [providerKeys, currentKey, providerStatus])
}

/** Cursor auth banner copy when provider_status.cursor.ok === false. */
export function useCursorAuthStatus(): ProviderStatus | null {
  const { providerStatus } = useProviderCatalog()
  const st = providerStatus.cursor
  if (!st || st.ok) return null
  return st
}

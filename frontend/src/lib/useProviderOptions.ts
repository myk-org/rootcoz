import { useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import type { AiModelsResponse, ProviderStatus } from '@/types'
import { buildProviderOptions, type AiProviderOption } from '@/lib/aiProviders'

export type { ProviderStatus }

type CatalogState = {
  providerKeys: string[]
  providers: AiModelsResponse['providers']
  /** @deprecated Use providerKeys. */
  enabled: string[]
  providerStatus: Record<string, ProviderStatus>
}

const EMPTY_CATALOG: CatalogState = { providerKeys: [], providers: {}, enabled: [], providerStatus: {} }

/** Shared in-flight / completed catalog so concurrent hook mounts share one fetch. */
const catalogInflight = new Map<string, Promise<CatalogState>>()
const catalogCache = new Map<string, CatalogState>()
let catalogGeneration = 0
/** Mounted useProviderCatalog consumers — notified on cache reset. */
const catalogSubscribers = new Set<() => void>()

function catalogKeyFor(username: string, isAdmin: boolean, authenticated: boolean): string {
  if (!authenticated || !username) return 'anon'
  return `${username}:${isAdmin ? '1' : '0'}`
}

function loadProviderCatalog(cacheKey: string, forceServer: boolean): Promise<CatalogState> {
  const cached = catalogCache.get(cacheKey)
  if (cached) return Promise.resolve(cached)
  const inflight = catalogInflight.get(cacheKey)
  if (inflight) return inflight
  const generation = catalogGeneration
  const req = api
    .get<AiModelsResponse>(`/api/ai-models?force_server_credentials=${forceServer}`)
    .then((res) => {
      const providers = res.providers ?? {}
      const providerKeys = Object.keys(providers)
      const next: CatalogState = {
        providerKeys,
        providers,
        enabled: providerKeys.filter((p) => (providers[p] ?? []).length > 0),
        providerStatus: res.provider_status ?? {},
      }
      if (generation === catalogGeneration && catalogInflight.get(cacheKey) === req) {
        catalogCache.set(cacheKey, next)
        catalogInflight.delete(cacheKey)
      }
      return next
    })
    .catch((err) => {
      if (catalogInflight.get(cacheKey) === req) catalogInflight.delete(cacheKey)
      throw err
    })
  catalogInflight.set(cacheKey, req)
  return req
}

/**
 * Clear shared catalog cache and notify mounted useProviderCatalog consumers
 * so they refetch (login/logout/admin refresh/tests).
 */
export function resetProviderCatalogCache(): void {
  catalogGeneration++
  catalogInflight.clear()
  catalogCache.clear()
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
export function useProviderCatalog(forceServer = false): {
  providerKeys: string[]
  providers: AiModelsResponse['providers']
  /** @deprecated Use providerKeys. */
  enabled: string[]
  providerStatus: Record<string, ProviderStatus>
} {
  const { username, isAdmin, authenticated } = useAuth()
  const cacheKey = `${catalogKeyFor(username, isAdmin, authenticated)}:${forceServer ? 'server' : 'default'}`
  const [reloadToken, setReloadToken] = useState(0)
  const [state, setState] = useState<{ key: string; catalog: CatalogState }>(() => ({
    key: cacheKey, catalog: catalogCache.get(cacheKey) ?? EMPTY_CATALOG,
  }))

  useEffect(() => {
    const notify = () => setReloadToken((n) => n + 1)
    catalogSubscribers.add(notify)
    return () => {
      catalogSubscribers.delete(notify)
    }
  }, [])

  useEffect(() => {
    let ignore = false
    const generation = catalogGeneration
    loadProviderCatalog(cacheKey, forceServer)
      .then((next) => {
        if (!ignore && generation === catalogGeneration) setState({ key: cacheKey, catalog: next })
      })
      .catch(() => {
        if (!ignore && generation === catalogGeneration) setState({ key: cacheKey, catalog: EMPTY_CATALOG })
      })
    return () => {
      ignore = true
    }
  }, [cacheKey, forceServer, reloadToken])

  return state.key === cacheKey ? state.catalog : catalogCache.get(cacheKey) ?? EMPTY_CATALOG
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

/**
 * Cursor auth banner copy when provider_status.cursor.ok === false.
 * Pass the caller's credential mode so the banner matches the catalog the
 * caller actually selects models from.
 */
export function useCursorAuthStatus(forceServer = false): ProviderStatus | null {
  const { providerStatus } = useProviderCatalog(forceServer)
  const st = providerStatus.cursor
  if (!st || st.ok) return null
  return st
}
